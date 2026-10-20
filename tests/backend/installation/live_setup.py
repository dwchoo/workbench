"""G-WI-05: current noneditable WB artifact installs actual stable OMP, no model turn.

Run only when explicitly authorized. Reports stay in run-root; all mutable runtime
resources live under an owned 0700 directory below sticky /tmp.
"""
from __future__ import annotations

import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
METADATA = 'https://build.stencil.so/api/products/omp/latest/linux-x86_64?channel=stable'


def fetch_official_metadata():
    with urlopen(Request(METADATA, headers={'User-Agent': 'omp-workbench-live-check/0.1'}), timeout=30) as response:
        return json.loads(response.read(2 * 1024 * 1024 + 1))


def process_inventory(private):
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            argv = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            if str(private) in '\0'.join(argv):
                found.append({'pid': int(entry.name), 'argv': argv})
        except (FileNotFoundError, ProcessLookupError):
            pass
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, required=True)
    args = parser.parse_args()
    output = args.run_root.absolute()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    private = Path(tempfile.mkdtemp(prefix='wb-setup-live-', dir='/tmp'))
    private.chmod(0o700)
    report = {'private_root': str(private), 'owner': os.geteuid(), 'source': str(ROOT),
              'scope': 'Linux x86_64 glibc, SDK imports/load only; real models/auth unverified',
              'commands': [], 'checks': {}, 'error': None}
    (output / 'private-runtime.json').write_text(json.dumps({'path': str(private), 'owner': os.geteuid(), 'mode': '0700'}, indent=2))
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
           'UV_PYTHON_DOWNLOADS': 'never', 'UV_NO_PROGRESS': '1'}
    for variable in ('HOME', 'TMPDIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'UV_CACHE_DIR',
                     'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
        path = private / variable.lower()
        path.mkdir(mode=0o700)
        env[variable] = str(path)
    project = private / 'research'
    project.mkdir(mode=0o700)
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('uv executable required')

    def run(argv, name, timeout=180):
        stdout = output / (name + '-stdout.log')
        stderr = output / (name + '-stderr.log')
        with stdout.open('wb') as out, stderr.open('wb') as err:
            process = subprocess.Popen(argv, cwd=project, env=env, stdout=out, stderr=err,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                status = process.wait(timeout=timeout)
            except BaseException:
                try:
                    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    owned = True
                except ChildProcessError:
                    owned = False
                if owned:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=10)
                raise
        report['commands'].append({'argv': list(map(str, argv)), 'pid': process.pid, 'returncode': status,
                                   'stdout': str(stdout), 'stderr': str(stderr)})
        if status:
            raise RuntimeError(f'{name} failed: {status}; see {stderr}')
        return stdout.read_text()

    try:
        snapshot = private / 'source'
        snapshot.mkdir()
        for name in ('src', 'omp_bridge', 'build_support'):
            shutil.copytree(ROOT / name, snapshot / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '_assets'))
        for name in ('pyproject.toml', 'MANIFEST.in'):
            shutil.copy2(ROOT / name, snapshot / name)
        dist = output / 'dist'
        run([uv, 'build', '-v', '--sdist', '--wheel', '--out-dir', str(dist), str(snapshot)], 'build')
        wheel = next(dist.glob('*.whl'))
        run([uv, 'tool', 'install', '--python', sys.executable, str(wheel)], 'install-wb')
        python = private / 'uv_tool_dir/workbench/bin/python'
        metadata_program = '''import json, importlib.metadata as m
from workbench.assets import package_root, canonical_path
print(json.dumps({'package': str(package_root()), 'helper': str(canonical_path('omp_bridge/setup_probe.ts')),
 'distributions': sorted([{'name': d.metadata['Name'], 'version': d.version} for d in m.distributions()], key=lambda x:x['name'])}))'''
        installed = json.loads(run([str(python), '-I', '-c', metadata_program], 'installed-runtime'))
        assert installed['package'].startswith(str(private / 'uv_tool_dir'))
        helper = Path(installed['helper'])
        assert helper.read_bytes() == (ROOT / 'omp_bridge/setup_probe.ts').read_bytes()
        report['installed_wb'] = installed
        build_log = (output / 'build-stderr.log').read_text()
        report['build_setuptools_versions_observed'] = sorted(set(re.findall(r'setuptools==([0-9][0-9A-Za-z.+-]*)', build_log)))
        report['artifact_sha256'] = {path.name: sha256(path.read_bytes()).hexdigest() for path in dist.iterdir() if path.is_file()}
        before = fetch_official_metadata()
        (output / 'official-before.json').write_text(json.dumps(before, indent=2))
        command = private / 'uv_tool_bin_dir/wb'
        result = json.loads(run([str(command), 'setup', '--json'], 'setup-install', timeout=1000))
        selection_file = private / 'xdg_config_home/omp-workbench/setup.json'
        selection = json.loads(selection_file.read_text())
        after = fetch_official_metadata()
        (output / 'official-after.json').write_text(json.dumps(after, indent=2))
        expected = [metadata for metadata in (before, after)
                    if metadata['build']['version'] == selection['version']
                    and metadata['file']['sha256'] == selection['binary_sha256']]
        binary = Path(result['path'])
        with binary.open('rb') as stream:
            actual_sha = file_digest(stream, 'sha256').hexdigest()
        repeat = json.loads(run([str(command), 'setup', '--json'], 'setup-reuse', timeout=90))
        report['result'], report['repeat'], report['selection'] = result, repeat, selection
        report['checks'] = {
            'installed_wb_command': installed['package'].startswith(str(private / 'uv_tool_dir')),
            'installed_setup_helper': helper.is_file() and '_assets/omp_bridge/setup_probe.ts' in str(helper),
            'new_stable_install': result['status'] == 'installed' and bool(expected),
            'official_integrity_and_size': bool(expected) and actual_sha == expected[0]['file']['sha256']
                and binary.stat().st_size == expected[0]['file']['size'],
            'private_managed_prefix': binary.is_relative_to(private / 'xdg_data_home/omp-workbench/tools')
                and binary.stat().st_mode & 0o777 == 0o700,
            'actual_sdk_loaded': result['sdk']['status'] == 'loaded' and result['sdk']['version'] == selection['version']
                and result['sdk']['probe_sha256'] == sha256(helper.read_bytes()).hexdigest(),
            'repeat_reused_same_binary': repeat['status'] == 'reused' and repeat['path'] == result['path'],
            'project_untouched': list(project.iterdir()) == [],
            'no_auth_completion_claim': result['real_models'] == 'unverified' and 'not checked' in result['login_guidance'],
            'probe_temporaries_removed': list((private / 'tmpdir').iterdir()) == [],
        }
        assert all(report['checks'].values()), report['checks']
    except BaseException as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        report['remaining_processes'] = process_inventory(private)
        if not report['remaining_processes']:
            # Keep installed WB/OMP and receipts for review; discard only owned rebuildable caches.
            report['cache_cleanup'] = []
            for name in ('uv_cache_dir', 'source', 'home/.omp/natives', 'xdg_cache_home'):
                path = private / name
                if path.exists():
                    size = sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())
                    shutil.rmtree(path)
                    report['cache_cleanup'].append({'path': str(path), 'logical_bytes': size})
        report['retained_logical_bytes'] = sum(p.stat().st_size for p in private.rglob('*') if p.is_file() and not p.is_symlink())
        report['private_root_mode'] = oct(private.stat().st_mode & 0o777)
        (output / 'receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'run_root': str(output), 'private_root': str(private), 'checks': report['checks'],
                          'error': report['error'], 'remaining_processes': report['remaining_processes']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
