"""G-WI-06: noneditable installed CLI, real retained OMP and loopback-only project lifecycle.

Owns only scratch projects, fake auth and processes whose exact identity was observed.
No credential content, paid provider, user backend or OMP download is used.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import select
import shlex
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
# Runtime binary is an explicit recipe input; never search the user's environment/home.



def state(pid, ticks):
    try:
        directory = Path('/proc') / str(pid)
        if directory.stat().st_uid != os.geteuid():
            return 'unknown'
        fields = (directory / 'stat').read_bytes().rsplit(b') ', 1)[1].split()
        if int(fields[19]) != ticks:
            return 'unknown'
        return 'ended' if fields[0] in (b'Z', b'X') else 'alive'
    except FileNotFoundError:
        return 'ended'
    except (OSError, ValueError, IndexError):
        return 'unknown'


def signal_exact(pid, ticks, number):
    if state(pid, ticks) != 'alive':
        return
    descriptor = os.pidfd_open(pid)
    try:
        if state(pid, ticks) == 'alive':
            signal.pidfd_send_signal(descriptor, number)
    finally:
        os.close(descriptor)


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
    def do_POST(self):
        self.server.requests += 1
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        frames = [
            {"id": "wi05", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "WI05_LOOPBACK_REPLY"}, "finish_reason": None}]},
            {"id": "wi05", "object": "chat.completion.chunk", "created": 0, "model": "scripted",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}]
        if self.server.requests == 1 and getattr(self.server, 'helper_command', None):
            frames[0]['choices'][0]['delta'] = {'role': 'assistant', 'tool_calls': [
                {'index': 0, 'id': 'wi05-helper', 'type': 'function',
                 'function': {'name': 'bash', 'arguments': json.dumps({'command': self.server.helper_command})}}]}
            frames[1]['choices'][0]['finish_reason'] = 'tool_calls'
        body = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class InstalledRun:
    def __init__(self, output, omp=None):
        chosen = omp or os.environ.get('WB_LIVE_OMP')
        if not chosen:
            raise ValueError('supply --omp PATH or WB_LIVE_OMP for the verified test binary')
        self.omp = Path(chosen).resolve(strict=True)
        if not self.omp.is_file() or not os.access(self.omp, os.X_OK):
            raise ValueError('test OMP must be an existing executable: ' + str(self.omp))
        self.output = output
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.private = Path(tempfile.mkdtemp(prefix='wb-project-live-', dir='/tmp'))
        self.private.chmod(0o700)
        self.report = {'private_root': str(self.private), 'commands': [], 'checks': {}, 'error': None}
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TERM': 'xterm-256color',
                    'PYTHONDONTWRITEBYTECODE': '1', 'UV_PYTHON_DOWNLOADS': 'never'}
        for key in ('HOME', 'TMPDIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'UV_CACHE_DIR',
                    'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR'):
            path = self.private / key.lower()
            path.mkdir(mode=0o700)
            self.env[key] = str(path)
        self.command = self.private / 'uv_tool_bin_dir/wb'
        self.python = self.private / 'uv_tool_dir/workbench/bin/python'
        self.projects = []
        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.provider.requests = 0
        self.thread = threading.Thread(target=self.provider.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        self.thread.start()
        self.model = self.private / 'loopback.yml'
        self.model.write_text('startup:\n  setupWizard: false\nproviders:\n  wi-project:\n'
            f'    baseUrl: http://127.0.0.1:{self.provider.server_port}/v1\n'
            '    api: openai-completions\n    auth: none\n    models:\n'
            '      - id: scripted\n        name: lifecycle fixture\n        contextWindow: 32768\n        maxTokens: 1024\n')

    def run(self, argv, name, *, cwd=None, env=None, timeout=180, check=True):
        with (self.output / (name + '.out')).open('wb') as out, (self.output / (name + '.err')).open('wb') as err:
            child = subprocess.Popen(list(map(str, argv)), cwd=cwd or self.private,
                env=env or self.env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            try:
                status = child.wait(timeout=timeout)
            except BaseException:
                # WNOWAIT retains the direct child's PID/session until after cleanup signals.
                try:
                    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    pass
                else:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait(timeout=10)
                raise
        self.report['commands'].append({'name': name, 'argv': list(map(str, argv)), 'returncode': status})
        stdout = (self.output / (name + '.out')).read_text()
        stderr = (self.output / (name + '.err')).read_text()
        if check and status:
            raise AssertionError(f'{name}: exit {status}: {stderr[-4000:]} {stdout[-4000:]}')
        return status, stdout, stderr

    def install(self):
        snapshot = self.private / 'source'
        snapshot.mkdir()
        for name in ('src', 'omp_bridge', 'build_support'):
            shutil.copytree(ROOT / name, snapshot / name,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '_assets'))
        for name in ('pyproject.toml', 'MANIFEST.in'):
            shutil.copy2(ROOT / name, snapshot / name)
        uv = shutil.which('uv')
        assert uv
        self.run([uv, 'build', '--wheel', '--out-dir', self.private / 'dist', snapshot], 'build')
        self.run([uv, 'tool', 'install', '--python', sys.executable,
                  next((self.private / 'dist').glob('*.whl'))], 'install')
        assert self.omp.is_file()
        self.env['WORKBENCH_OMP'] = str(self.omp)
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            self.env[key] = 'http://127.0.0.1:9'
        self.env.update(NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        auth = Path(self.env['HOME']) / '.omp/agent'
        auth.mkdir(parents=True, mode=0o700)
        (auth / 'agent.db').write_bytes(b'')

    def project(self, name):
        project = self.private / name
        project.mkdir(mode=0o700)
        self.projects.append(project)
        code = ('import os,json; from pathlib import Path; '
                'from workbench.backend.project import resolve_project_context,initialize_project; '
                'c=initialize_project(resolve_project_context(Path.cwd(),os.environ),confirm=lambda _:True,'
                'interactive=True,environment=os.environ); '
                'print(json.dumps({"project_root":str(c.project_root),"data_root":str(c.data_root),'
                '"runtime_root":str(c.runtime_root),"project_id":c.project_id}))')
        _, stdout, _ = self.run([self.python, '-I', '-c', code], 'init-' + str(len(self.projects)), cwd=project)
        agent = project / '.wb/omp-root/agent'
        agent.mkdir(parents=True, mode=0o700)
        (agent / 'models.yml').write_bytes(self.model.read_bytes())
        return project, json.loads(stdout)

    def start(self, project, name, *, cwd=None, timeout=120, env=None):
        options = ['start', '--no-attach', '--timeout', str(timeout)]
        options += [f'--omp-arg={value}' for value in ('--no-pty', '--no-rules', '--model',
                                                     'wi-project/scripted', '--config', str(self.model))]
        return self.run([self.command, *options], name, cwd=cwd or project, timeout=timeout + 20, env=env)

    def status(self, project, name, *, env=None):
        _, stdout, _ = self.run([self.command, 'status', '--json'], name, cwd=project, env=env, timeout=20)
        return json.loads(stdout)['snapshot']

    def research_environment(self):
        poison = self.private / 'research modules'
        poison.mkdir()
        self.canary = self.private / 'unexpected-research-import'
        (poison / 'sitecustomize.py').write_text(
            'from pathlib import Path\nPath(' + repr(str(self.canary)) + ').touch()\n')
        (poison / 'workbench').mkdir()
        (poison / 'workbench/__init__.py').write_text('raise RuntimeError("research WB imported")\n')
        commands = self.private / 'research bin'
        commands.mkdir()
        self.command_canary = self.private / 'unexpected-research-python'
        for name in ('python', 'python3', 'readlink'):
            path = commands / name
            path.write_text(f'''#!/bin/sh
printf '%s\\n' '{name}' "$@" >> "{self.command_canary}"
/usr/bin/readlink /proc/$PPID/exe >> "{self.command_canary}"
printf '%s\\n' END >> "{self.command_canary}"
exit 33
''')
            path.chmod(0o700)
        self.env.update(PATH=str(commands) + ':/usr/bin:/bin', PYTHONPATH=str(poison),
                        VIRTUAL_ENV='/research/venv with spaces', CONDA_PREFIX='/research/conda',
                        WORKBENCH_INSTALL_PYTHON='/spoofed/research/python')

    def environment_checks(self, project, snapshot):
        manager = snapshot['panes']['manager_omp']['process']['pid']
        host = snapshot['panes']['host_shell']['process']['pid']
        def environment(pid):
            return dict(item.decode().split('=', 1) for item in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
                        if b'=' in item)
        manager_env, host_env = environment(manager), environment(host)
        assert manager_env['WORKBENCH_INSTALL_PYTHON'] == str(self.python)
        assert 'WORKBENCH_INSTALL_PYTHON' not in host_env
        keys = ('PATH', 'PYTHONPATH', 'VIRTUAL_ENV', 'CONDA_PREFIX')
        assert all(host_env[key] == self.env[key] for key in keys)
        code = 'from workbench.assets import canonical_path; print(canonical_path("omp_bridge/skills/workbench-report/scripts/report_io.py"))'
        _, stdout, _ = self.run([self.python, '-I', '-c', code], 'report-helper-location')
        helper = Path(stdout.strip())
        before = self.command_canary.read_bytes() if self.command_canary.exists() else b''
        if before:
            expected = ('python\n-c\nimport sys;sys.exit(0)\n' + str(self.omp.resolve()) + '\nEND\n').encode()
            assert before.replace(expected, b'') == b'', before
            self.report['external_omp_python_availability_probe'] = before.decode()
        self.run([manager_env['WORKBENCH_INSTALL_PYTHON'], '-I', helper, '--help'],
                 'actual-manager-report-helper', env=manager_env)
        helper_result = self.private / 'actual-manager-bash-helper.out'
        self.provider.helper_command = ('"${WORKBENCH_INSTALL_PYTHON:?WB installation Python unavailable}" -I '
                                        + shlex.quote(str(helper)) + ' --help > ' + shlex.quote(str(helper_result)))
        missing = dict(manager_env)
        missing.pop('WORKBENCH_INSTALL_PYTHON')
        code, _, error = self.run(['/bin/sh', '-c', self.provider.helper_command],
                                   'missing-manager-install-python', env=missing, check=False)
        assert code != 0 and 'WB installation Python unavailable' in error
        self.helper_result = helper_result
        assert not self.canary.exists()
        assert (self.command_canary.read_bytes() if self.command_canary.exists() else b'') == before
        self.report['research_environment'] = {key: host_env[key] for key in keys}
        self.report['manager_install_python'] = manager_env['WORKBENCH_INSTALL_PYTHON']
        self.report['checks']['installed_manager_helper_and_host_research_env'] = True

    def bare_pty(self, project, name, *, refusal=False, env=None, spawned=None):
        """Real CLI TTY; retain unreaped direct-child identity before cleanup signal."""
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
        argv = [str(self.command)] + (['start', '--no-attach'] if refusal else [])
        child = subprocess.Popen(['/usr/bin/setsid', '--ctty', *argv], cwd=project, env=env or self.env,
                                 stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        if spawned is not None:
            spawned.set()
        output = bytearray()
        stop = threading.Event()
        def drain():
            while not stop.is_set():
                try:
                    if select.select([master], [], [], .05)[0]:
                        chunk = os.read(master, 65536)
                        if not chunk:
                            break
                        output.extend(chunk)
                except OSError:
                    break
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 15
            status_number = 0
            while time.monotonic() < deadline:
                if refusal:
                    ready = b'[y/N]' in output
                else:
                    code, text, error = self.run([self.command, 'status', '--json'],
                        name + '-status-' + str(status_number), cwd=project, env=env, check=False)
                    status_number += 1
                    assert code in (0, 3), error
                    ready = code == 0 and json.loads(text)['snapshot']['attached']
                if ready:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('CLI TTY did not reach confirmation/attach')
            os.write(master, b'\r' if refusal else b'\x1dq')
            status = child.wait(timeout=15)
            assert status == (2 if refusal else 0), (status, output[-2000:])
            if refusal:
                assert os.fsencode(project) in output and not (project / '.wb').exists()
                assert not (project / '.gitignore').exists()
            self.report['checks'][name] = True
        finally:
            if child.returncode is None:
                try:
                    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    pass
                else:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait(timeout=5)
            stop.set()
            reader.join(2)
            os.close(master)
            (self.output / (name + '.pty')).write_bytes(output)

    def bound_sockets(self, records):
        roots = {record[key] for record in records for key in ('runtime_root', 'omp_runtime_root') if record.get(key)}
        paths = []
        for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
            fields = line.split(maxsplit=7)
            if len(fields) == 8 and any(fields[7].startswith(root + '/') for root in roots):
                path = Path(fields[7])
                assert len(os.fsencode(path)) <= 107, path
                assert stat.S_ISSOCK(path.stat().st_mode), path
                paths.append({'path': str(path), 'bytes': len(os.fsencode(path))})
        assert all(any(item['path'] == record['ui_socket'] for item in paths) and
                   any(item['path'] == record['bridge_socket'] for item in paths) for record in records)
        self.report['actual_bound_sockets'] = paths

    def installed_supervisor_check(self):
        code = r'''import os,sys,time,shlex,json
from pathlib import Path
from uuid import uuid4
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell
root=Path(sys.argv[1]);result=root/'managed-child-environment'
shell=PersistentShell(user_environment=dict(os.environ),choice=ShellChoice('bash','/usr/bin/bash'))
def until(condition):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        state=shell.poll(.02);shell.display_bytes()
        if condition(state):return state
    raise AssertionError(shell.snapshot())
try:
    shell.send_user(b'wb-handoff\n');until(lambda s:s['parent_mode']=='control_wait');shell.claim_manager()
    state=shell.snapshot()
    control={'portVersion':2,'kind':'ShellControl','payload':{'parentPid':shell.parent_pid,'generation':state['generation'],'ownerEpoch':state['owner_epoch'],'requestId':str(uuid4()),'approvalHash':'e'*64,'phase':'accepted'}}
    automation={'portVersion':2,'kind':'AutomationState','payload':{'paused':False,'cancelled':False,'metadataHealthy':True,'approvalValid':True}}
    script="printf '%s\\n' \"$PYTHONPATH\" \"$VIRTUAL_ENV\" > "+shlex.quote(str(result))
    shell.submit(control,script,automation);until(lambda s:s['lifecycle']['input_barrier'])
    shell.release_input();value=until(lambda s:s['lifecycle']['control_returned'])
    assert result.read_text().splitlines()==[os.environ['PYTHONPATH'],os.environ['VIRTUAL_ENV']]
    print(json.dumps({'actual_installed_supervisor':True,'main_exit':value['lifecycle']['main_exit'],'child_research_env_preserved':True}))
finally:shell.close()
'''
        _, text, _ = self.run([self.python, '-I', '-c', code, self.private], 'installed-managed-supervisor')
        assert not self.canary.exists() and not self.command_canary.exists()
        self.report['installed_supervisor'] = json.loads(text)
        self.report['checks']['installed_supervisor_preimport_isolation'] = True

    def restart_different_runtime(self, project):
        """Real SQLite lock delays real backend record publication; no product hooks."""
        import hashlib
        import sqlite3
        data = project / '.wb'
        old_record = json.loads((data / 'backend.json').read_text())
        old_ref = old_record['processes']['backend']
        before_requests = self.provider.requests
        sessions = data / 'omp-root/agent/sessions'
        def observe_sessions(phase):
            files = {}
            for path in sessions.rglob('*.jsonl'):
                content = path.read_bytes()
                files[str(path)] = hashlib.sha256(content).hexdigest()
                target = self.output / ('session-' + phase) / path.relative_to(sessions)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(content)
                target.chmod(0o600)
            self.report.setdefault('session_observations', {})[phase] = files
            return files
        before_sessions = observe_sessions('before-stop')
        self.run([self.command, 'shutdown', '--yes', '--json'], 'normal-stop-A', cwd=project, timeout=65)
        deadline = time.monotonic() + 10
        while state(old_ref['pid'], old_ref['start_ticks']) != 'ended' and time.monotonic() < deadline:
            time.sleep(.02)
        assert state(old_ref['pid'], old_ref['start_ticks']) == 'ended'
        stopped_sessions = observe_sessions('after-stop')
        assert set(before_sessions).issubset(stopped_sessions)
        for path in before_sessions:
            relative = Path(path).relative_to(sessions)
            original = (self.output / 'session-before-stop' / relative).read_bytes()
            stopped = Path(path).read_bytes()
            assert stopped.startswith(original), path  # every prior byte/entry/ID stays in order
            appended = stopped[len(original):]
            for line in appended.splitlines():
                event = json.loads(line)
                assert event.get('type') == 'custom' and event.get('customType') == 'session_exit', event
                assert event.get('data', {}).get('reason') == 'sigterm', event
                assert event.get('data', {}).get('kind') == 'signal', event
        previous = (data / 'backend.json').read_bytes()
        old_admission = json.loads((data / 'backend-starting.json').read_text())
        new_base = self.private / 'restart-runtime'
        new_base.mkdir(mode=0o700)
        new_env = dict(self.env, XDG_RUNTIME_DIR=str(new_base))
        database = sqlite3.connect(data / 'tasks.sqlite3', isolation_level=None)
        database.execute('BEGIN EXCLUSIVE')
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            first = pool.submit(self.start, project, 'normal-restart-A', timeout=120, env=new_env)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    admission = json.loads((data / 'backend-starting.json').read_text())
                except FileNotFoundError:
                    time.sleep(.01)
                    continue  # inactive old admission retired before no-clobber publication
                if admission['startup_id'] != old_admission['startup_id']:
                    break
                time.sleep(.01)
            else:
                raise AssertionError('restart did not publish a fresh admission')
            lock_info = (data / 'backend.lock').stat()
            assert admission['lock'] == {'device': lock_info.st_dev, 'inode': lock_info.st_ino}
            # Admission precedes gate/exec/lock. Wait for the actual kernel lock
            # before testing the ended-record+held-lock interval.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                rows = [line.split() for line in Path('/proc/locks').read_text().splitlines()]
                owned = any(parts[1:4] == ['FLOCK', 'ADVISORY', 'WRITE'] and
                            int(parts[4]) == admission['backend']['pid'] for parts in rows if len(parts) >= 8)
                if owned:
                    break
                time.sleep(.01)
            else:
                raise AssertionError('admitted restart child did not acquire the kernel lock')
            spawned = threading.Event()
            second = pool.submit(self.bare_pty, project, 'restart-different-runtime-attach',
                                 env=self.env, spawned=spawned)
            assert spawned.wait(3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                waiting = any(item['name'].startswith('restart-different-runtime-attach-status-')
                              and item['returncode'] == 3 for item in self.report['commands'])
                if waiting:
                    break
                time.sleep(.01)
            else:
                raise AssertionError('no public pending status while delayed by SQLite lock')
            assert (data / 'backend.json').read_bytes() == previous
            database.rollback()  # let the real core reconcile/open path continue
            first.result(timeout=150)
            second.result(timeout=35)
        finally:
            if database.in_transaction:
                database.rollback()  # release fixture lock before waiting for any failed future
            database.close()
            pool.shutdown(wait=True)
        snapshot = self.status(project, 'normal-restarted-A-status', env=self.env)
        record = json.loads((data / 'backend.json').read_text())
        assert record['processes']['backend'] == snapshot['backend']['process'] or all(
            record['processes']['backend'][key] == snapshot['backend']['process'][key]
            for key in ('pid', 'start_ticks'))
        assert record['processes']['backend']['pid'] != old_ref['pid']
        assert record['runtime_root'] != old_record['runtime_root']
        assert Path(record['runtime_root']).is_relative_to(new_base)
        assert snapshot['automation']['paused'] and not snapshot['boot']['confirmation_required']
        assert self.provider.requests == before_requests
        assert observe_sessions('after-restart') == stopped_sessions
        self.report['restart'] = {'previous_record_sha256': hashlib.sha256(previous).hexdigest(),
            'old_backend': old_ref, 'new_backend': record['processes']['backend'],
            'new_admission': admission, 'same_boot': snapshot['boot'],
            'pending_status_observed': True, 'provider_requests_unchanged': before_requests,
            'prior_bytes_preserved_through_shutdown': before_sessions,
            'post_shutdown_sessions_unchanged_on_restart': stopped_sessions}
        self.report['checks']['actual_stop_restart_delayed_publication_different_runtime_attach_no_replay'] = True
        return record

    def private_processes(self):
        """Only exact same-UID processes with an owned fixture path in cwd/argv/env."""
        found = {}
        for path in Path('/proc').iterdir():
            if not path.name.isdigit() or int(path.name) == os.getpid():
                continue
            try:
                if path.stat().st_uid != os.geteuid():
                    continue
                fields = (path / 'stat').read_bytes().rsplit(b') ', 1)[1].split()
                if fields[0] in (b'Z', b'X'):
                    continue
                prefix = os.fsencode(self.private) + b'/'
                cwd = os.fsencode(os.readlink(path / 'cwd')) + b'/'
                argv = (path / 'cmdline').read_bytes()
                environment = (path / 'environ').read_bytes()
                if cwd.startswith(prefix) or prefix in argv or prefix in environment:
                    found[int(path.name)] = int(fields[19])
            except (OSError, ValueError, IndexError):
                continue
        return found

    def exercise(self, project, context, label='A'):
        """Installed public client: one fixture turn, durable session and pause reattach."""
        code = r'''import json,sys,time
from pathlib import Path
from workbench.backend.client import UiClient
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId
project=Path(sys.argv[1]);record=json.loads((project/'.wb/backend.json').read_text())
with UiClient(Path(record['ui_socket']),expected_process=record['processes']['backend']) as c:
    assert c.attach()['ok']
    assert c.input(PaneId.MANAGER_OMP,('WI05_LOOPBACK_TURN_'+sys.argv[2]).encode())['ok']
    assert c.input(PaneId.MANAGER_OMP,b'\r')['ok']
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        sessions=list((project/'.wb/omp-root/agent/sessions').rglob('*.jsonl'))
        if any('WI05_LOOPBACK_REPLY' in p.read_text() for p in sessions):break
        c.pump(.1)
    else:raise AssertionError('loopback answer missing from durable project session')
    pause=c.request(ClientType.PAUSE);assert pause['ok'],pause
    c.detach()
with UiClient(Path(record['ui_socket']),expected_process=record['processes']['backend']) as c:
    assert c.attach()['ok']
    snapshot=c.snapshot();assert snapshot['automation']['paused'],snapshot['automation']
    assert snapshot['backend']['pid']==record['processes']['backend']['pid']
    c.detach()
print(json.dumps({'session_files':[str(p) for p in sessions],'paused':True,'backend_pid':snapshot['backend']['pid']}))
'''
        before = self.provider.requests
        _, text, _ = self.run([self.python, '-I', '-c', code, project, label], 'turn-pause-reattach-' + label, timeout=50)
        self.report['turn_pause_reattach_' + label] = json.loads(text)
        expected = 2 if label == 'A' and getattr(self.provider, 'helper_command', None) else 1
        assert self.provider.requests == before + expected, self.provider.requests
        if label == 'A' and expected == 2:
            assert 'usage:' in self.helper_result.read_text()
            assert not self.canary.exists()
            self.report['checks']['actual_manager_bash_uses_trusted_install_python'] = True
        import sqlite3
        history = project / '.wb/omp-root/agent/history.db'
        with sqlite3.connect('file:' + str(history) + '?mode=ro', uri=True) as database:
            assert database.execute('SELECT prompt FROM history WHERE prompt=?',
                                    ('WI05_LOOPBACK_TURN_' + label,)).fetchall() == [('WI05_LOOPBACK_TURN_' + label,)]
        record = json.loads((project / '.wb/backend.json').read_text())
        xdg = Path(record['omp_runtime_root']) / 'xdg-data/omp'
        assert (xdg / 'history.db').resolve() == history
        auth = Path(self.env['HOME']) / '.omp/agent/agent.db'
        assert (xdg / 'agent.db').resolve() == auth
        assert (auth.parent / 'agent.db-shm').is_file()
        assert not os.path.lexists(xdg / 'agent.db-shm')
        assert not os.path.lexists(xdg / 'history.db-wal')
        self.report['persistent_locations_' + label] = {
            'history_reopened': str(history), 'auth_link_target': str(auth),
            'auth_sidecars': [str(path) for path in auth.parent.glob('agent.db*')],
            'history_sidecars': [str(path) for path in history.parent.glob('history.db*')]}
        self.report['checks']['one_loopback_turn_durable_session_pause_reattach_no_replay'] = True

    def cleanup(self):
        known = {}
        runtimes = []
        for index, project in enumerate(self.projects):
            data = project / '.wb'
            try:
                record = json.loads((data / 'backend.json').read_text())
            except (OSError, ValueError):
                continue
            if record.get('data_dir') != str(data):
                raise AssertionError('cleanup refused a foreign record')
            runtimes += [(Path(value), data, record.get('project_id')) for key in ('runtime_root', 'omp_runtime_root')
                         if (value := record.get(key)) and record.get('project_id')]
            for ref in record.get('processes', {}).values():
                if isinstance(ref, dict) and type(ref.get('pid')) is int and type(ref.get('start_ticks')) is int:
                    known[ref['pid']] = ref['start_ticks']
            try:
                self.run([self.command, 'shutdown', '--yes', '--json', '--data-dir', data], f'cleanup-{index}',
                         cwd=project, timeout=65, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        known.update(self.private_processes())
        for pid, ticks in known.items():
            signal_exact(pid, ticks, signal.SIGTERM)
        deadline = time.monotonic() + 6
        while any(state(pid, ticks) == 'alive' for pid, ticks in known.items()) and time.monotonic() < deadline:
            time.sleep(.05)
        for pid, ticks in known.items():
            signal_exact(pid, ticks, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while any(state(pid, ticks) == 'alive' for pid, ticks in known.items()) and time.monotonic() < deadline:
            time.sleep(.05)
        self.report['remaining_processes'] = [{'pid': pid, 'ticks': ticks, 'state': state(pid, ticks)}
            for pid, ticks in known.items() if state(pid, ticks) != 'ended']
        self.report['unrecorded_private_processes'] = self.private_processes()
        self.provider.shutdown()
        self.provider.server_close()
        self.thread.join(5)
        if not self.report['remaining_processes'] and not self.report['unrecorded_private_processes']:
            self.report['regenerable_cache_cleanup'] = []
            for runtime, data, project_id in runtimes:
                try:
                    info = runtime.lstat()
                    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                    assert stat.S_IMODE(info.st_mode) == 0o700 and runtime == runtime.resolve()
                    assert json.loads((runtime / 'runtime.json').read_text()) == {
                        'schema_version': 1, 'data_root': str(data), 'project_id': project_id}
                    removed = []
                    for name in ('cache', 'run', 'bun-transpiler-cache', 'agent-cache', 'terminal-sessions',
                                 'model-cache', 'xdg-cache', 'daemons', 'xdg-data/omp/natives',
                                 'xdg-data/omp/predict'):
                        path = runtime / name
                        if path.is_symlink():
                            raise AssertionError(f'cache cleanup refused unexpected symlink: {path}')
                        if path.exists():
                            shutil.rmtree(path)
                            removed.append(name)
                    for pattern in ('models.db*', 'skill-descriptions.db*'):
                        for path in (runtime / 'xdg-data/omp').glob(pattern):
                            path.unlink()
                            removed.append(str(path.relative_to(runtime)))
                    self.report['regenerable_cache_cleanup'].append({'runtime': str(runtime), 'removed': removed})
                except (OSError, ValueError, AssertionError) as error:
                    self.report['regenerable_cache_cleanup'].append({'runtime': str(runtime), 'refused': str(error)})
            for name in ('uv_cache_dir', 'source'):

                path = self.private / name
                if path.exists():
                    shutil.rmtree(path)
        self.report['provider_requests'] = self.provider.requests
        (self.output / 'receipt.json').write_text(json.dumps(self.report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, required=True)
    parser.add_argument('--omp', type=Path, help='verified executable (or WB_LIVE_OMP); no automatic discovery/download')
    args = parser.parse_args()
    chosen = args.omp or os.environ.get('WB_LIVE_OMP')
    if not chosen or not Path(chosen).is_file() or not os.access(chosen, os.X_OK):
        parser.error('supply existing executable --omp PATH or WB_LIVE_OMP (loopback/fake auth only)')
    run = InstalledRun(args.run_root.absolute(), chosen)
    try:
        run.install()
        run.research_environment()
        refusal = run.private / 'first-project-refused'
        refusal.mkdir()
        run.bare_pty(refusal, 'first_init_default_no', refusal=True)
        run.installed_supervisor_check()
        project, context = run.project('research-A')
        child = project / 'experiments'
        child.mkdir()
        with ThreadPoolExecutor(max_workers=2) as pool:
            starts = [pool.submit(run.start, project, 'start-A-' + str(index), cwd=child, timeout=120)
                      for index in range(2)]
            for start in starts:
                start.result()
        assert (project / '.wb/backend.log').read_text().count('starting in') == 1
        run.report['checks']['simultaneous_same_project_one_backend'] = True
        snapshot = run.status(child, 'status-A')
        record = json.loads((project / '.wb/backend.json').read_text())
        assert record['project_id'] == context['project_id']
        assert record['project_dir'] == str(project)
        assert record['runtime_root'] == context['runtime_root']
        assert snapshot['backend']['project_dir'] == str(project)
        assert Path(record['ui_socket']).parent == Path(context['runtime_root'])
        assert all(snapshot['panes'][name]['alive'] for name in ('manager_omp', 'worker_omp', 'host_shell'))
        assert snapshot['phase'] == 'ready', snapshot['reason']
        assert all(snapshot['bridge'][role]['pid_matches_pane'] for role in ('manager', 'worker'))
        assert snapshot['omp_isolation']['ok'], snapshot['omp_isolation']
        assert run.provider.requests == 0
        run.report['snapshot_A'] = snapshot
        run.report['checks']['installed_project_start'] = True
        cache = project / '.wb/omp-root/bun-transpiler-cache'
        assert cache.is_symlink() and not cache.resolve().is_relative_to(project / '.wb'), str(cache)
        run.report['checks']['runtime_cache_outside_project'] = True
        run.environment_checks(project, snapshot)
        run.exercise(project, context)
        pid_A = snapshot['backend']['pid']
        run.bare_pty(child, 'bare_wb_attach_detach_preserves_pause')
        after = run.status(project, 'status-A-after-bare')
        assert after['backend']['pid'] == pid_A and after['automation']['paused']
        changed_runtime = run.private / 'changed-runtime'
        changed_runtime.mkdir(mode=0o700)
        changed = run.status(child, 'status-A-changed-runtime', env=dict(run.env, XDG_RUNTIME_DIR=str(changed_runtime)))
        assert changed['backend']['pid'] == pid_A
        assert changed['backend']['runtime_root'] == record['runtime_root']
        assert not list(changed_runtime.iterdir())
        run.report['checks']['durable_record_beats_changed_runtime_environment'] = True
        project_B, context_B = run.project('연구-' + '긴경로' * 20)
        assert len(os.fsencode(project_B / '.wb/ui.sock')) > 107
        run.start(project_B, 'start-B-long', timeout=120)
        snapshot_B = run.status(project_B, 'status-B')
        record_B = json.loads((project_B / '.wb/backend.json').read_text())
        assert snapshot_B['phase'] == 'ready', snapshot_B['reason']
        assert record_B['project_id'] != record['project_id'] and snapshot_B['backend']['pid'] != pid_A
        assert record_B['runtime_root'] != record['runtime_root']
        run.exercise(project_B, context_B, 'B')
        for name in ('tasks.sqlite3', 'important-logs', 'raw-logs'):
            path_A, path_B = project / '.wb' / name, project_B / '.wb' / name
            assert path_A.exists() and path_B.exists() and not path_A.samefile(path_B)
        sessions_A = b''.join(path.read_bytes() for path in (project / '.wb/omp-root/agent/sessions').rglob('*.jsonl'))
        sessions_B = b''.join(path.read_bytes() for path in (project_B / '.wb/omp-root/agent/sessions').rglob('*.jsonl'))
        assert b'WI05_LOOPBACK_TURN_A' in sessions_A and b'WI05_LOOPBACK_TURN_B' not in sessions_A
        assert b'WI05_LOOPBACK_TURN_B' in sessions_B and b'WI05_LOOPBACK_TURN_A' not in sessions_B
        for label, root in (('A', project), ('B', project_B)):
            _, raw, _ = run.run([run.command, 'logs', 'raw-status', '--json'], 'raw-status-' + label, cwd=root)
            run.report['raw_status_' + label] = json.loads(raw)
        run.report['checks']['two_projects_durable_db_raw_important_session_separation_and_long_path'] = True
        legacy = run.private / 'legacy'
        legacy.mkdir()
        run.projects.append(legacy)
        data = legacy / '.wb'
        agent = data / 'omp-root/agent'
        agent.mkdir(parents=True, mode=0o700)
        data.chmod(0o700)
        (agent / 'models.yml').write_bytes(run.model.read_bytes())
        argv = [run.command, 'start', '--data-dir', data, '--no-attach']
        argv += [f'--omp-arg={value}' for value in ('--no-pty', '--no-rules', '--model', 'wi-project/scripted')]
        run.run(argv, 'legacy-start', cwd=legacy)
        _, text, _ = run.run([run.command, 'status', '--data-dir', data, '--json'], 'legacy-explicit-status', cwd=project_B)
        legacy_snapshot = json.loads(text)['snapshot']
        legacy_record = json.loads((data / 'backend.json').read_text())
        assert legacy_record['runtime_root'] == str(data) and legacy_record['project_id'] is None
        assert legacy_snapshot['backend']['project_dir'] == str(legacy)
        assert legacy_record['ui_socket'] == str(data / 'ui.sock')
        assert not (data / 'project.json').exists() and not (data / 'omp-root/bun-transpiler-cache').is_symlink()
        run.bound_sockets([record, record_B, legacy_record])
        run.report['initial_bound_sockets'] = run.report['actual_bound_sockets']
        restarted = run.restart_different_runtime(project)
        run.bound_sockets([restarted, record_B, legacy_record])
        run.report['actual_bound_sockets'] += run.report['initial_bound_sockets']
        assert run.provider.requests == 3 and not run.canary.exists()
        run.report['checks']['explicit_unmarked_legacy_in_place_and_recorded_project_root'] = True
        run.report['records'] = [record, record_B, legacy_record, restarted]

    except BaseException as error:
        run.report['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        run.cleanup()
    print(json.dumps({'run_root': str(run.output), 'checks': run.report['checks'],
                      'remaining_processes': run.report['remaining_processes']}))


if __name__ == '__main__':
    main()
