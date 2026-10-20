"""Opt-in real uv/Git self-replacement feasibility probe (no user installation).

This is an exploratory probe, not an implementation of ``wb update``. It reuses
InstalledRun's bounded command runner, installs the real WB packaging plus a
probe-only module in a private Git snapshot, and never starts OMP or a backend.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/backend/installation'))
from live_project_sessions import InstalledRun  # noqa: E402


# All imports precede self-replacement. In-memory code and the open lock file
# survive uv removing/recreating the installed package; new imports use a fresh
# installed interpreter. This module exists only in the private snapshot.
RUNNER = '''import fcntl, json, os, signal, subprocess, sys, time
REVISION = "previous"
config = json.loads(open(sys.argv[1]).read())
lock = os.open(config["lock"], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
event = os.open(config["events"], os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
interruptions = []
def emit(**values):
    os.write(event, (json.dumps(values) + "\\n").encode())
    os.fsync(event)
def interrupted(number, _frame):
    interruptions.append(number)
    emit(phase="interrupted", signal=number)
signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, interrupted)
def wait_for(path):
    deadline = time.monotonic() + 45
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise TimeoutError(path)
        time.sleep(.02)
emit(phase="locked", pid=os.getpid(), revision=REVISION, python=sys.executable)
wait_for(config["go"])
with open(config["uv_out"], "wb") as out, open(config["uv_err"], "wb") as err:
    child = subprocess.Popen(config["argv"], stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    try:
        status = child.wait(timeout=120)
    except BaseException:
        # The outer harness owns this runner's entire process group. Leave
        # descendants in that group for its verified cleanup, including when
        # this runner exits. It retains our unreaped PID until group cleanup.
        raise
emit(phase="installed", uv_returncode=status, pid=os.getpid(), revision=REVISION)
wait_for(config["release"])
exitcode = status or (128 + interruptions[-1] if interruptions else 0)
emit(phase="reported", success=status == 0, uv_returncode=status, exitcode=exitcode)
os.close(event)
os.close(lock)
raise SystemExit(exitcode)
'''

IDENTITY = '''import hashlib, importlib.metadata as m, json, pathlib, sys
import workbench, workbench._update_probe_marker as marker, pyte, wcwidth
from workbench.assets import canonical_path
d = m.distribution("workbench")
asset = canonical_path("omp_bridge/skills/workbench-report/scripts/report_io.py")
print(json.dumps({"python":sys.executable,"prefix":sys.prefix,
 "module":workbench.__file__,"revision":marker.REVISION,
 "direct_url":json.loads(d.read_text("direct_url.json")),
 "installer":d.read_text("INSTALLER"),
 "asset_sha256":hashlib.sha256(pathlib.Path(asset).read_bytes()).hexdigest()}))
'''


class UpdateProbe(InstalledRun):
    """Reuse the installed harness without its unrelated OMP/provider setup."""

    def __init__(self, output: Path, uv: Path, seed_cache: Path):
        self.output = output.resolve()
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.private = Path(tempfile.mkdtemp(prefix='wb-up-', dir='/tmp'))
        self.private.chmod(0o700)
        self.uv = uv.resolve(strict=True)
        self.seed_cache = seed_cache.resolve(strict=True)
        self.report = {'private_root': str(self.private), 'commands': [], 'checks': {},
                       'error': None, 'probe_exception': 'Exploratory actual runtime feasibility; no product change or invented RED.'}
        self.report['probe_source_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.report['seed_cache'] = str(self.seed_cache)
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                    'PYTHONDONTWRITEBYTECODE': '1', 'UV_PYTHON_DOWNLOADS': 'never',
                    'UV_OFFLINE': '1', 'GIT_TERMINAL_PROMPT': '0',
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        for key in ('HOME', 'TMPDIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'UV_CACHE_DIR',
                    'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR'):
            path = self.private / key.lower()
            path.mkdir(mode=0o700)
            self.env[key] = str(path)
        self.command = self.private / 'uv_tool_bin_dir/wb'
        self.python = self.private / 'uv_tool_dir/workbench/bin/python'
        self.lock = self.private / 'xdg_runtime_dir/update.lock'
        self.children = []

    def prepare(self):
        self.run([self.uv, '--version'], 'uv-version')
        # Copy, never hardlink, the existing cache. uv can mutate only this copy.
        shutil.copytree(self.seed_cache, self.env['UV_CACHE_DIR'], dirs_exist_ok=True)
        source = self.private / 'source'
        source.mkdir()
        for name in ('src', 'omp_bridge', 'build_support'):
            shutil.copytree(ROOT / name, source / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '_assets'))
        for name in ('pyproject.toml', 'MANIFEST.in'):
            shutil.copy2(ROOT / name, source / name)
        (source / 'src/workbench/_update_probe.py').write_text(RUNNER)
        marker = source / 'src/workbench/_update_probe_marker.py'
        self.run(['git', 'init', '-q', source], 'git-init')
        commits = []
        for revision in ('previous', 'target'):
            marker.write_text('REVISION = ' + repr(revision) + '\n')
            self.run(['git', '-C', source, 'add', '.'], 'git-add-' + revision)
            self.run(['git', '-C', source, '-c', 'user.name=WU01 fixture', '-c',
                      'user.email=wu01@invalid', '-c', 'commit.gpgsign=false',
                      'commit', '-qm', revision], 'git-commit-' + revision)
            _, stdout, _ = self.run(['git', '-C', source, 'rev-parse', 'HEAD'], 'git-ref-' + revision)
            commits.append(stdout.strip())
        self.source = source
        self.report['commits'] = dict(zip(('previous', 'target'), commits))
        self.report['expected_asset_sha256'] = hashlib.sha256(
            (source / 'omp_bridge/skills/workbench-report/scripts/report_io.py').read_bytes()).hexdigest()
        self.run(self.install_argv(commits[0]), 'initial-install')

    def install_argv(self, ref):
        return [str(self.uv), 'tool', 'install', '--force', '--python', sys.executable,
                'git+' + self.source.as_uri() + '@' + ref]

    def identity(self, name):
        _, stdout, _ = self.run([self.python, '-I', '-c', IDENTITY], name)
        identity = json.loads(stdout)
        receipt = self.private / 'uv_tool_dir/workbench/uv-receipt.toml'
        identity['receipt'] = receipt.read_text()
        (self.output / (name + '.json')).write_text(json.dumps(identity, indent=2) + '\n')
        assert identity['installer'].strip() == 'uv'
        assert identity['python'] == str(self.python)
        assert identity['asset_sha256'] == self.report['expected_asset_sha256']
        assert not identity['direct_url'].get('dir_info', {}).get('editable', False)
        assert identity['direct_url']['vcs_info']['vcs'] == 'git'
        return identity

    def locked(self):
        fd = os.open(self.lock, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def wait_phase(self, child, events, phase):
        deadline = time.monotonic() + 145
        while time.monotonic() < deadline:
            rows = [json.loads(row) for row in events.read_text().splitlines()] if events.exists() else []
            if any(row['phase'] == phase for row in rows):
                return rows
            status = self.exit_status(child)
            if status is not None:
                raise AssertionError(f'runner exited {status} before {phase}')
            time.sleep(.02)
        raise TimeoutError(phase)

    @staticmethod
    def exit_status(child):
        # WNOWAIT keeps the session/group leader PID reserved even after exit.
        status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if status is None:
            return None
        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status

    @staticmethod
    def group_members(group):
        members = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_bytes().rsplit(b') ', 1)[1].split()
                if int(fields[2]) == group:
                    if entry.stat().st_uid != os.geteuid():
                        raise RuntimeError('unexpected process group owner')
                    members.append({'pid': int(entry.name), 'start_ticks': int(fields[19]),
                                    'state': fields[0].decode()})
            except FileNotFoundError:
                continue
        return members

    def replace(self, name, ref, expected_success):
        config = {key: str(self.private / (name + '-' + key)) for key in ('go', 'release')}
        events = self.output / (name + '-events.jsonl')
        config.update(lock=str(self.lock), events=str(events), argv=self.install_argv(ref),
                      uv_out=str(self.output / (name + '-uv.out')),
                      uv_err=str(self.output / (name + '-uv.err')))
        config_path = self.private / (name + '.json')
        config_path.write_text(json.dumps(config))
        with (self.output / (name + '-runner.out')).open('wb') as out, (self.output / (name + '-runner.err')).open('wb') as err:
            child = subprocess.Popen([self.python, '-I', '-m', 'workbench._update_probe', config_path],
                                     cwd=self.private, env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, start_new_session=True)
            self.children.append(child)
            self.wait_phase(child, events, 'locked')
            assert self.locked(), 'contender acquired fence before install'
            Path(config['go']).touch()
            rows = self.wait_phase(child, events, 'installed')
            assert self.exit_status(child) is None and self.locked(), 'fence lost during replacement/reporting'
            installed = rows[-1]
            assert (installed['uv_returncode'] == 0) is expected_success, installed
            if not expected_success:
                # A signal arriving while a failed install is being reported
                # must not silently release the maintenance fence.
                os.kill(child.pid, signal.SIGINT)
                self.wait_phase(child, events, 'interrupted')
                assert self.exit_status(child) is None and self.locked()
            observed = self.identity(name + '-identity')
            status, _, _ = self.run([self.command, '--help'], name + '-wb-help', check=False)
            self.report['checks'][name] = {'fence_before': True, 'fence_after': True,
                'same_runner_pid': rows[0]['pid'] == installed['pid'],
                'loaded_revision_after': installed['revision'], 'fresh_identity': observed,
                'entrypoint_returncode': status, 'uv_returncode': installed['uv_returncode'],
                'stdin': 'DEVNULL (EOF)', 'signal_while_reporting': not expected_success}
            Path(config['release']).touch()
            deadline = time.monotonic() + 15
            while (status := self.exit_status(child)) is None:
                if time.monotonic() > deadline:
                    raise TimeoutError('runner exit')
                time.sleep(.02)
            assert status == installed['uv_returncode']
            assert not self.locked(), 'fence leaked after runner exit'
            final = [json.loads(row) for row in events.read_text().splitlines()][-1]
            assert final['success'] is expected_success
            assert status == 0 if expected_success else status != 0
            return observed

    def exercise(self):
        before = self.identity('before')
        assert before['revision'] == 'previous'
        assert before['direct_url']['vcs_info']['commit_id'] == self.report['commits']['previous']
        self.run([self.command, '--help'], 'before-wb-help')
        after = self.replace('self-replace', self.report['commits']['target'], True)
        assert after['revision'] == 'target'
        assert after['direct_url']['vcs_info']['commit_id'] == self.report['commits']['target']
        failed = self.replace('invalid-ref', 'wu01-deliberately-missing-ref', False)
        self.report['invalid_ref_install_state'] = failed
        # This observed failure need not imply that every uv failure is atomic.
        assert failed['direct_url']['vcs_info']['commit_id'] == self.report['commits']['target']

    def cleanup(self):
        groups, unknowns = [], []
        for child in self.children:
            try:
                if child.returncode is not None:
                    raise RuntimeError('group leader was already reaped; ownership unknown')
                self.exit_status(child)  # Still our unreaped child: no PID/group reuse.
                os.killpg(child.pid, signal.SIGKILL)
                deadline = time.monotonic() + 10
                while True:
                    members = self.group_members(child.pid)
                    if all(member['state'] in ('Z', 'X') for member in members):
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError('process group remains running')
                    time.sleep(.02)
                groups.append({'group': child.pid, 'stopped_members': members})
                # Only reap after signalling/observing the reserved group.
                child.wait(timeout=10)
            except (OSError, RuntimeError, subprocess.TimeoutExpired, TimeoutError) as error:
                unknowns.append({'group': child.pid, 'error': str(error)})
        self.report['cleanup_groups'] = groups
        self.report['cleanup_unknowns'] = unknowns
        self.report['owned_children_stopped'] = not unknowns
        self.report['probe_source_unchanged'] = self.report['probe_source_sha256'] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if not unknowns:
            shutil.rmtree(self.private)
        self.report['private_root_removed'] = not self.private.exists()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--uv', type=Path, required=True)
    parser.add_argument('--seed-cache', type=Path, required=True)
    args = parser.parse_args()
    probe = UpdateProbe(args.output, args.uv, args.seed_cache)
    try:
        probe.prepare()
        probe.exercise()
    except BaseException as error:
        probe.report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        probe.cleanup()
        (probe.output / 'report.json').write_text(json.dumps(probe.report, indent=2) + '\n')
        if not probe.report['owned_children_stopped']:
            raise RuntimeError('process cleanup unverified; private root retained')


if __name__ == '__main__':
    main()
