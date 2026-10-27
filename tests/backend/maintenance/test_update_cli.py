"""Public CLI behavior; installation/process doubles remain external boundaries."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench.backend import cli


class UpdateCLI(unittest.TestCase):
    def test_development_checkout_is_explained_and_refused(self):
        with tempfile.TemporaryDirectory(prefix='wu02-') as private:
            output, error = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {'HOME': private, 'XDG_STATE_HOME': private+'/state',
                    'XDG_RUNTIME_DIR': private+'/runtime'}, clear=True), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                try:
                    status = cli.main(['update'])
                except SystemExit as exc:
                    status = exc.code
            self.assertEqual(status, 1, error.getvalue())
            self.assertIn('uv 관리 Git 설치', error.getvalue())
            self.assertFalse((Path(private)/'state').exists())


class InstalledFixture(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='wu02-')
        self.addCleanup(self.scratch.cleanup)
        self.home = Path(self.scratch.name)
        self.prefix = self.home/'tools/workbench'
        self.prefix.mkdir(parents=True)
        self.site = self.prefix/'lib/python3.12/site-packages'
        self.site.mkdir(parents=True)
        self.meta = self.site/'workbench-0.1.0.dist-info'
        self.meta.mkdir()
        (self.meta/'METADATA').write_text('Name: workbench\nVersion: 0.1.0\n')
        (self.meta/'INSTALLER').write_text('uv\n')
        self.old = '1'*40
        self.target = '2'*40
        self.url = 'https://example.invalid/wb.git'
        self.write_install(self.old)
        self.runtime = self.home/'runtime'
        self.runtime.mkdir(mode=0o700)
        self.proc = self.home/'proc'
        self.proc.mkdir()
        self.env = {'HOME': str(self.home), 'PATH':'/usr/bin:/bin',
                    'XDG_STATE_HOME':str(self.home/'state'), 'XDG_RUNTIME_DIR':str(self.runtime),
                    'UV_TOOL_DIR':str(self.prefix.parent)}
        self.clients = {}
        self.applied = self.home/'installer-applied.json'

    def write_install(self, commit):
        import json
        (self.meta/'direct_url.json').write_text(json.dumps({'url':self.url,
            'vcs_info':{'vcs':'git','commit_id':commit,'requested_revision':commit}}))
        (self.prefix/'uv-receipt.toml').write_text('[tool]\nrequirements = [{name="workbench", git="'+self.url+'?rev='+commit+'"}]\n')

    def tool(self, argv, *, env=None, **kwargs):
        import json
        import subprocess
        argv = list(map(str, argv))
        if len(argv)>1 and argv[1]=='start':
            return subprocess.CompletedProcess(argv,1,'','업데이트 또는 backend 시작 준비 중입니다.')
        if argv[0].endswith('git'):
            stdout = self.target+'\n' if 'rev-parse' in argv else ''
        elif argv[0].endswith('uv'):
            if argv[1:] == ['tool', 'dir']:
                stdout = str(self.prefix.parent)+'\n'
            elif env and env.get('UV_TOOL_DIR') != str(self.prefix.parent):
                stdout = ''
            else:
                self.applied.write_text(json.dumps(argv))
                self.write_install(self.target)
                stdout = ''
        else:
            stdout = json.dumps({'commit':self.target,'verified':True})
        return subprocess.CompletedProcess(argv, 0, stdout, '')

    def invoke(self, argv=('update',), *, stdin=None, tool=None):
        import importlib.metadata
        import sys
        from workbench.backend import update, maintenance
        output, error = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, self.env, clear=True))
            stack.enter_context(patch.object(maintenance, 'FALLBACK_RUNTIME', self.home/'fallback', create=True))
            stack.enter_context(patch.object(maintenance, 'FENCE_BASE', self.home/'fence', create=True))
            stack.enter_context(patch.object(maintenance, 'PROC', self.proc, create=True))
            stack.enter_context(patch.object(sys, 'prefix', str(self.prefix)))
            stack.enter_context(patch.object(sys, 'executable', str(self.prefix/'bin/python')))
            stack.enter_context(patch.object(update.importlib.metadata, 'distribution',
                return_value=importlib.metadata.PathDistribution(self.meta)))
            stack.enter_context(patch('shutil.which', side_effect=lambda name: '/fixture/'+name))
            stack.enter_context(patch.object(update, 'command', side_effect=tool or self.tool, create=True))
            stack.enter_context(patch.object(update, 'UiClient', side_effect=lambda socket, **kw: self.clients[str(socket)], create=True))
            stack.enter_context(patch.object(cli, 'UiClient', side_effect=lambda socket, **kw: self.clients[str(socket)]))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(error))
            if stdin is not None:
                stack.enter_context(patch.object(sys, 'stdin', stdin))
            status = cli.main(list(argv))
        return status, output.getvalue(), error.getvalue()

    def test_idle_installed_update_pins_ref_and_reports_verified_success(self):
        import json
        status, output, error = self.invoke(('update', '--ref', 'release-1'))
        self.assertEqual(status, 0, error)
        self.assertIn(self.old, output)
        self.assertIn(self.target, output)
        self.assertIn('업데이트 완료', output)
        self.assertIn('@'+self.target, json.loads(self.applied.read_text())[-1])
        manifests = list((self.home/'state/omp-workbench/updates').glob('*/manifest.json'))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())['installation'], 'verified')

    def project(self, name, active):
        import json, sqlite3, subprocess, sys, uuid
        from workbench.backend.boot import read_boot_id
        from workbench.backend.paths import DataLayout, InstanceLock, write_private_json
        from workbench.backend.runtime_paths import resolve_runtime_root, ensure_runtime_root
        project = self.home/name
        project.mkdir(mode=0o700)
        data = project/'.wb'
        data.mkdir(mode=0o700)
        identity = str(uuid.uuid4())
        write_private_json(data/'project.json', {'schema_version':1, 'state':'completed',
            'project_root':str(project),'data_root':str(data),'project_id':identity})
        runtime = resolve_runtime_root(data, identity, self.env)
        ensure_runtime_root(runtime, data, identity)
        layout = DataLayout(data, runtime_root=runtime)
        lock = InstanceLock(layout.lock)
        lock.acquire()
        self.addCleanup(lock.release)
        backend = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        def end():
            if backend.poll() is None:
                backend.kill()
                backend.wait(timeout=5)
        self.addCleanup(end)
        ticks = int(Path(f'/proc/{backend.pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        reference = {'pid':backend.pid,'start_ticks':ticks}
        record = {'data_dir':str(data),'project_dir':str(project),'project_id':identity,
            'runtime_root':str(runtime),'ui_socket':str(layout.ui_socket),'bridge_socket':str(layout.bridge_socket),
            'uid':os.geteuid(),'boot_id':read_boot_id(),'phase':'running','processes':{'backend':reference}}
        write_private_json(layout.record, record)
        db = sqlite3.connect(data/'records.sqlite3')
        db.executescript("CREATE TABLE revisions(id TEXT, revision INTEGER, body TEXT); INSERT INTO revisions VALUES('work-a',1,'ORIGINAL_ONE'),('work-a',2,'ORIGINAL_TWO');")
        db.close()
        (data/'records.sqlite3').chmod(0o600)
        sessions = data/'omp-root/agent/sessions'
        sessions.mkdir(parents=True, mode=0o700)
        (sessions/'manager.jsonl').write_text('MANAGER_SESSION_RAW\n')
        (sessions/'worker.jsonl').write_text('WORKER_SESSION_RAW\n')
        fixture = self
        class Client:
            closing = None
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def snapshot(self):
                return {'backend':dict(record, pid=backend.pid,process=reference),
                    'task':{'task_id':'task-a','run_id':'run-a','revision':2},
                    'bridge':{'manager':{'session_id':'manager-a'},'worker':{'session_id':'worker-a'}},
                    'phase':'running','panes':{},'holds':[{'reason':'user_pause'}]}
            def request(self, kind, **kwargs):
                from workbench.contracts.ui_v1 import ClientType
                if kind == ClientType.SHUTDOWN_REQUEST:
                    return {'token':'fixture-token','active':active,'processes':{'backend':reference},'scope_bound':True}
                assert kind == ClientType.SHUTDOWN_CONFIRM
                assert kwargs['token'] == 'fixture-token'
                end()
                lock.release()
                record.update(phase='stopped', shutdown={'verified':True})
                write_private_json(layout.record, record)
                self.closing = {'result':{'verified':True}}
                return {'ok':True}
        self.clients[str(layout.ui_socket)] = Client()
        return project, data, backend

    def test_active_decline_preserves_process_data_and_creates_no_backup(self):
        import hashlib
        project, data, backend = self.project('project-a', [{'kind':'task_run','task_id':'task-a','run_id':'run-a'}])
        before = {str(p.relative_to(data)):hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()}
        class Tty(io.StringIO):
            def isatty(self): return True
        status, output, error = self.invoke(stdin=Tty('n\n'))
        self.assertEqual(status, 1, output+error)
        self.assertIn('task-a', output)
        self.assertIn(str(project), output)
        self.assertIsNone(backend.poll())
        self.assertFalse(self.applied.exists())
        self.assertFalse((self.home/'state/omp-workbench/updates').exists())
        self.assertEqual(before, {str(p.relative_to(data)):hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()})

    def test_approved_two_projects_stop_and_preserve_private_snapshot(self):
        import hashlib, json, sqlite3
        a, data_a, backend_a = self.project('project-a', [{'kind':'task_run','task_id':'task-a','run_id':'run-a'}])
        b, data_b, backend_b = self.project('project-b', [])
        for project, data in ((a,data_a),(b,data_b)):
            (project/'source.py').write_text('SOURCE_SENTINEL')
            (project/'result.bin').write_bytes(b'EXPERIMENT_RESULT')
            (data/'omp-root/agent/agent.db.moved-legacy').write_bytes(b'CREDENTIAL_MUST_NOT_BE_READ')
            (data/'omp-root/agent/agent.db').symlink_to(project/'result.bin')
            cache = data/'omp-root/cache'
            cache.mkdir()
            (cache/'rebuild').write_text('REGENERABLE')
        original = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for project in (a,b) for p in (project/'source.py',project/'result.bin')}
        class Tty(io.StringIO):
            def isatty(self): return True
        status, output, error = self.invoke(stdin=Tty('y\n'))
        self.assertEqual(status, 0, output+error)
        self.assertIsNotNone(backend_a.poll())
        self.assertIsNotNone(backend_b.poll())
        manifests = list((self.home/'state/omp-workbench/updates').glob('*/manifest.json'))
        manifest = json.loads(manifests[0].read_text())
        self.assertTrue(manifest['backup_complete'])
        self.assertEqual(len(manifest['projects']), 2)
        for item in manifest['projects']:
            snapshot = manifests[0].parent/item['backup']
            db = sqlite3.connect(snapshot/'records.sqlite3')
            self.assertEqual(db.execute('SELECT revision,body FROM revisions ORDER BY revision').fetchall(), [(1,'ORIGINAL_ONE'),(2,'ORIGINAL_TWO')])
            db.close()
            self.assertEqual((snapshot/'omp-root/agent/sessions/manager.jsonl').read_text(),'MANAGER_SESSION_RAW\n')
            self.assertEqual(item['snapshot']['task']['run_id'],'run-a')
            self.assertEqual(item['snapshot']['bridge']['manager']['session_id'],'manager-a')
            self.assertFalse((snapshot/'omp-root/agent/agent.db').exists())
            self.assertFalse((snapshot/'omp-root/agent/agent.db.moved-legacy').exists())
            self.assertFalse((snapshot/'omp-root/cache').exists())
            self.assertEqual(snapshot.stat().st_mode & 0o777,0o700)
            self.assertEqual((snapshot/'records.sqlite3').stat().st_mode & 0o777,0o600)
            marker = json.loads((Path(item['data_root'])/'update-resume.json').read_text())
            self.assertEqual(marker['backup'],str(manifests[0].parent))
            self.assertIn('wb start', marker['resume'])
        self.assertEqual(original,{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in original})

    def test_update_fence_blocks_update_and_start_without_changes(self):
        import fcntl
        fence = self.home/'fence'
        fence.mkdir(mode=0o700)
        descriptor = os.open(fence/'update.lock', os.O_CREAT|os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX|fcntl.LOCK_NB)
            for argv in (('update',), ('start','--no-attach')):
                status, output, error = self.invoke(argv)
                self.assertEqual(status, 1, output+error)
                self.assertIn('업데이트', error)
            self.assertFalse(self.applied.exists())
            self.assertFalse((self.home/'state').exists())
        finally:
            os.close(descriptor)

    def test_unbound_current_user_backend_candidate_is_not_treated_as_idle(self):
        import shutil
        project, data, backend = self.project('project-a', [])
        for directory in (self.runtime/'omp-workbench').iterdir():
            shutil.rmtree(directory)
        process = self.proc/str(backend.pid)
        process.mkdir()
        (process/'cmdline').write_bytes(b'python\0-I\0-c\0from workbench.backend.cli import main\0_backend\0--data-dir\0'+os.fsencode(data)+b'\0')
        status, output, error = self.invoke()
        self.assertEqual(status,1,output+error)
        self.assertFalse(self.applied.exists())
        self.assertIsNone(backend.poll())
        self.assertFalse((self.home/'state/omp-workbench/updates').exists())

    def test_remaining_writer_refuses_backup_and_install(self):
        project, data, backend = self.project('project-a', [])
        writer = self.proc/'400001'
        (writer/'fd').mkdir(parents=True)
        (writer/'fdinfo').mkdir()
        (writer/'cmdline').write_bytes(b'fixture-writer\0')
        (writer/'fd/7').symlink_to(data/'records.sqlite3')
        (writer/'fdinfo/7').write_text('flags:\t0100002\n')
        status, output, error = self.invoke()
        self.assertEqual(status,1,output+error)
        self.assertIsNotNone(backend.poll())
        self.assertFalse(self.applied.exists())
        import json
        manifest = json.loads(next((self.home/'state/omp-workbench/updates').glob('*/manifest.json')).read_text())
        self.assertFalse(manifest['backup_complete'])
        self.assertEqual(manifest['installation'],'not_started')

    def unreadable_session_process(self, pid, comm):
        entry = self.proc/str(pid)
        (entry/'fd').mkdir(parents=True)
        (entry/'cmdline').write_bytes(comm.encode()+b'\0')
        (entry/'comm').write_text(comm+'\n')
        (entry/'fd').chmod(0)
        self.addCleanup((entry/'fd').chmod, 0o700)

    def test_unreadable_session_processes_are_listed_and_recorded_but_do_not_block(self):
        import json
        project, data, backend = self.project('project-a', [])
        self.unreadable_session_process(32774, 'systemd')
        self.unreadable_session_process(32778, '(sd-pam)')
        status, output, error = self.invoke()
        self.assertEqual(status, 0, output+error)
        self.assertIn('업데이트 완료', output)
        lines = [line for line in output.splitlines() if line.startswith('확인하지 못한 process')]
        self.assertEqual(lines, ['확인하지 못한 process(쓰기 여부 미확인, 건너뜀): 32774 systemd, 32778 (sd-pam)'])
        manifest = json.loads(next((self.home/'state/omp-workbench/updates').glob('*/manifest.json')).read_text())
        expected = [{'pid':32774, 'comm':'systemd'}, {'pid':32778, 'comm':'(sd-pam)'}]
        self.assertEqual(manifest['projects'][0]['writer_checks'],
                         [{'check':'before_backup', 'unverified_processes':expected},
                          {'check':'after_backup', 'unverified_processes':expected}])

    def test_long_unverified_process_list_is_truncated_to_one_line(self):
        project, data, backend = self.project('project-a', [])
        for offset in range(12):
            self.unreadable_session_process(32700+offset, f'agent{offset}')
        status, output, error = self.invoke()
        self.assertEqual(status, 0, output+error)
        lines = [line for line in output.splitlines() if line.startswith('확인하지 못한 process')]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith('32707 agent7 외 4개'), lines[0])

    def test_idle_update_without_unverified_processes_prints_no_unverified_line(self):
        import json
        project, data, backend = self.project('project-a', [])
        status, output, error = self.invoke()
        self.assertEqual(status, 0, output+error)
        self.assertNotIn('확인하지 못한 process', output)
        manifest = json.loads(next((self.home/'state/omp-workbench/updates').glob('*/manifest.json')).read_text())
        self.assertEqual([check['unverified_processes'] for check in manifest['projects'][0]['writer_checks']], [[], []])

    def test_start_shows_maintenance_resume_without_replay_or_state_changes(self):
        import hashlib, json
        from workbench.backend.paths import write_private_json
        project, data, backend = self.project('project-a', [])
        marker = {'schema_version':1, 'backup':'/private/update-backup/run-a',
            'task':{'task_id':'task-a','run_id':'run-a','state':'unknown','revision':2},
            'sessions':{'manager':{'session_id':'manager-a'},'worker':{'session_id':'worker-a'}},
            'state':{'paused':True,'cancelled':True,'approval_hash':'KEEP_APPROVAL'},
            'resume':'wb start --data-dir '+str(data), 'shutdown':{'verified':True}}
        write_private_json(data/'update-resume.json', marker)
        before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()}
        status, output, error = self.invoke(('start','--no-attach','--data-dir',str(data)))
        self.assertEqual(status,0,error)
        for expected in ('업데이트 재개 안내','task-a','manager-a','worker-a','/private/update-backup/run-a','자동 재실행하지 않습니다'):
            self.assertIn(expected,output)
        self.assertIsNone(backend.poll())
        self.assertEqual(before,{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in before})

    def test_install_failure_reports_exit_and_private_backup_without_url_credentials(self):
        import json, subprocess
        secret = 'DO_NOT_PRINT_CREDENTIAL'
        self.url = 'https://git-user:'+secret+'@example.invalid/wb.git'
        self.write_install(self.old)
        def failure(argv, **kwargs):
            if str(argv[0]).endswith('uv') and '--offline' in argv:
                return subprocess.CompletedProcess(argv,17,'',self.url)
            return self.tool(argv,**kwargs)
        status, output, error = self.invoke(tool=failure)
        self.assertEqual(status,1,output+error)
        self.assertIn('uv exit 17',error)
        self.assertIn(self.old,error)
        self.assertIn(self.target,error)
        self.assertIn('수동 재설치',error)
        path = next((self.home/'state/omp-workbench/updates').glob('*/manifest.json'))
        manifest = json.loads(path.read_text())
        self.assertEqual(manifest['uv_returncode'],17)
        self.assertEqual(manifest['installation'],'unknown')
        self.assertFalse(manifest['complete'])
        self.assertTrue(manifest['backup_complete'])
        self.assertNotIn(secret,output+error+path.read_text())
        self.assertFalse(self.applied.exists())

    def test_legacy_credential_copy_is_excluded_without_reading(self):
        import json
        project, data, backend = self.project('project-a', [])
        secret = data/'auth.json.bak'
        secret.write_text('NEVER_BACK_UP_THIS_SECRET')
        secret.chmod(0)
        status, output, error = self.invoke()
        self.assertEqual(status,0,output+error)
        path = next((self.home/'state/omp-workbench/updates').glob('*/manifest.json'))
        manifest = json.loads(path.read_text())
        item = manifest['projects'][0]
        self.assertNotIn('auth.json.bak',[entry['path'] for entry in item['files']])
        self.assertIn('auth.json.bak',[entry['path'] for entry in item['excluded']])
        self.assertFalse((path.parent/item['backup']/'auth.json.bak').exists())
        self.assertNotIn('NEVER_BACK_UP_THIS_SECRET',path.read_text())

    def test_directory_swap_to_external_symlink_never_collects_external_file(self):
        project, data, backend = self.project('project-a', [])
        victim = data/'history'
        victim.mkdir()
        (victim/'original.txt').write_text('ORIGINAL_HISTORY')
        external = project/'private-external'
        external.mkdir()
        (external/'exfiltrated.txt').write_text('EXTERNAL_PRIVATE_CONTENT')
        original_listdir = os.listdir
        swapped = []
        def race(directory):
            named = Path(os.readlink('/proc/self/fd/'+str(directory))) if isinstance(directory,int) else Path(directory)
            if named == victim and not swapped:
                victim.rename(project/'old-history')
                victim.symlink_to(external, target_is_directory=True)
                swapped.append(True)
            return original_listdir(directory)
        with patch('os.listdir', side_effect=race):
            status, output, error = self.invoke()
        self.assertTrue(swapped)
        self.assertEqual(status,1,output+error)
        self.assertFalse(self.applied.exists())
        backups = self.home/'state/omp-workbench/updates'
        self.assertFalse(list(backups.rglob('exfiltrated.txt')), 'external private file was traversed and collected')

    def test_unreachable_backend_refuses_without_traceback_or_stop(self):
        from workbench.backend.client import ClientError
        project, data, backend = self.project('project-a', [])
        client = next(iter(self.clients.values()))
        def unavailable(*args, **kwargs):
            raise ClientError('fixture socket disconnected')
        client.request = unavailable
        status, output, error = self.invoke()
        self.assertEqual(status,1,error)
        self.assertIn('확인',error)
        self.assertIsNone(backend.poll())
        self.assertFalse(self.applied.exists())
        self.assertFalse((self.home/'state/omp-workbench/updates').exists())

    def test_pre_feature_target_is_refused_before_stopping_backend(self):
        import subprocess
        project, data, backend = self.project('project-a', [])
        def legacy(argv, **kwargs):
            if len(argv)>1 and argv[1]=='start':
                return subprocess.CompletedProcess(argv,2,'','OMP executable unavailable')
            return self.tool(argv,**kwargs)
        status, output, error = self.invoke(tool=legacy)
        self.assertEqual(status,1,output+error)
        self.assertIn('목표 WB',error)
        self.assertIsNone(backend.poll())
        self.assertFalse(self.applied.exists())
        self.assertFalse((self.home/'state/omp-workbench/updates').exists())

    def test_managed_caller_is_refused_before_stopping_any_backend(self):
        import json
        project,data,backend=self.project('managed-caller',[])
        record=json.loads((data/'backend.json').read_text())
        ticks=int(Path('/proc/self/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        # External process inventory adapter identifies this actual CLI caller
        # as the managed shell. No real backend owns/signals our test process.
        record['processes']['host_shell']={'pid':os.getpid(),'start_ticks':ticks}
        (data/'backend.json').write_text(json.dumps(record))
        before={str(path):path.read_bytes() for path in data.rglob('*') if path.is_file()}
        status,output,error=self.invoke()
        self.assertEqual(status,1,output+error)
        self.assertIn('외부 terminal',error)
        self.assertIsNone(backend.poll())
        self.assertFalse(self.applied.exists())
        self.assertFalse((self.home/'state/omp-workbench/updates').exists())
        self.assertEqual(before,{str(path):path.read_bytes() for path in data.rglob('*') if path.is_file()})


class WriterScan(unittest.TestCase):
    """require_no_writers over a fake /proc: unreadable same-uid processes are skipped and listed."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='wu-scan-')
        self.addCleanup(self.scratch.cleanup)
        base = Path(self.scratch.name)
        self.proc = base/'proc'
        self.proc.mkdir()
        self.root = base/'project/.wb'
        self.root.mkdir(parents=True)
        (self.root/'records.sqlite3').write_bytes(b'')

    def process(self, pid, comm, fds=()):
        entry = self.proc/str(pid)
        (entry/'fd').mkdir(parents=True)
        (entry/'fdinfo').mkdir()
        (entry/'comm').write_text(comm+'\n')
        for number, target, flags in fds:
            (entry/'fd'/str(number)).symlink_to(target)
            if flags is not None:
                (entry/'fdinfo'/str(number)).write_text(f'pos:\t0\nflags:\t{flags}\nmnt_id:\t1\n')
        return entry

    def deny(self, path):
        path.chmod(0)
        self.addCleanup(path.chmod, 0o700)

    def scan(self):
        from workbench.backend import maintenance
        with patch.object(maintenance, 'PROC', self.proc):
            return maintenance.require_no_writers(self.root)

    def test_unlistable_fd_directory_is_skipped_and_reported_with_pid_and_comm(self):
        self.deny(self.process(32774, 'systemd')/'fd')
        self.deny(self.process(32778, '(sd-pam)')/'fd')
        self.process(32790, 'bash', [(0, '/dev/null', '0100002')])
        self.assertEqual(self.scan(), [{'pid':32774, 'comm':'systemd'}, {'pid':32778, 'comm':'(sd-pam)'}])

    def test_process_exiting_mid_scan_is_skipped_silently(self):
        from workbench.backend import maintenance
        gone = self.process(41000, 'short-lived', [(3, self.root/'records.sqlite3', '0100002')])
        real = os.readlink
        def readlink(path, *args, **kwargs):
            if Path(path).parent.parent == gone:
                raise ProcessLookupError(3, 'No such process')
            return real(path, *args, **kwargs)
        with patch.object(maintenance.os, 'readlink', side_effect=readlink):
            self.assertEqual(self.scan(), [])

    def test_write_mode_fd_under_root_refuses_naming_pid_comm_and_path(self):
        from workbench.backend.paths import DataDirError
        self.deny(self.process(32774, 'systemd')/'fd')
        target = self.root/'records.sqlite3'
        self.process(52000, 'sqlite-writer', [(7, target, '0100002')])
        with self.assertRaises(DataDirError) as raised:
            self.scan()
        message = str(raised.exception)
        for expected in ('52000', 'sqlite-writer', str(target)):
            self.assertIn(expected, message)

    def test_fd_under_root_with_unreadable_or_unparsable_fdinfo_refuses_naming_pid_comm_and_path(self):
        from workbench.backend.paths import DataDirError
        target = self.root/'records.sqlite3'
        cases = {'unreadable': '0100000', 'unparsable': 'zz', 'missing-value': '', 'no-flags-line': None}
        for offset, (name, flags) in enumerate(cases.items()):
            with self.subTest(name):
                pid = 53000+offset
                entry = self.process(pid, 'reader-'+name, [(9, target, flags)])
                if name == 'unreadable':
                    (entry/'fdinfo'/'9').chmod(0)
                elif name == 'no-flags-line':
                    (entry/'fdinfo'/'9').write_text('pos:\t0\n')
                try:
                    with self.assertRaises(DataDirError) as raised:
                        self.scan()
                    message = str(raised.exception)
                    for expected in (str(pid), 'reader-'+name, str(target)):
                        self.assertIn(expected, message)
                finally:
                    (entry/'fdinfo'/'9').chmod(0o600)
                    import shutil
                    shutil.rmtree(entry)

    def test_read_only_fd_under_root_is_not_a_writer(self):
        self.process(54000, 'reader', [(4, self.root/'records.sqlite3', '0100000')])
        self.assertEqual(self.scan(), [])

    def test_unreadable_fd_links_are_skipped_and_reported(self):
        fd = self.process(32780, 'sshd', [(5, self.root/'records.sqlite3', '0100002')])/'fd'
        fd.chmod(0o400)  # listable but links unreadable (EACCES on readlink)
        self.addCleanup(fd.chmod, 0o700)
        self.assertEqual(self.scan(), [{'pid':32780, 'comm':'sshd'}])

    def test_other_scan_error_still_refuses_naming_pid_comm_and_errno(self):
        import errno
        from workbench.backend import maintenance
        from workbench.backend.paths import DataDirError
        self.process(55000, 'odd', [(1, '/dev/null', '0100002')])
        with patch.object(maintenance.os, 'readlink', side_effect=OSError(errno.EIO, 'I/O error')), \
                self.assertRaises(DataDirError) as raised:
            self.scan()
        for expected in ('55000', 'odd', f'errno {errno.EIO}'):
            self.assertIn(expected, str(raised.exception))
