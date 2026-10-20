"""공개 doctor CLI: 외부 설치 metadata만 대체하고 파일·version process는 실제."""
import contextlib
import fcntl
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from workbench.backend import cli


class DoctorCLI(unittest.TestCase):
    def setUp(self):
        scratch=tempfile.TemporaryDirectory(prefix='doctor-')
        self.addCleanup(scratch.cleanup)
        self.root=Path(scratch.name);self.cwd=self.root/'project';self.cwd.mkdir(mode=0o700)
        self.prefix=self.root/'tool';self.bin=self.prefix/'bin';self.bin.mkdir(parents=True)
        self.entry=self.bin/'wb';self.entry.write_text('# private console entrypoint\n');self.entry.chmod(0o700)
        (self.bin/'python').symlink_to(sys.executable)
        self.meta=self.prefix/'lib/workbench-0.1.dist-info';self.meta.mkdir(parents=True)
        (self.meta/'METADATA').write_text('Name: workbench\nVersion: 0.1.0\n')
        (self.meta/'INSTALLER').write_text('uv\n')
        (self.meta/'direct_url.json').write_text(json.dumps({'url':'file:///fixture/workbench.whl'}))
        for name,version in [('uv','uv 0.12.24'),('omp','omp/18.11.1')]:
            path=self.bin/name;path.write_text('#!/bin/sh\nprintf "%s\\n" '+repr(version)+'\n');path.chmod(0o700)
        self.env={'HOME':str(self.root/'home'),'PATH':str(self.bin)+':/usr/bin:/bin',
            'WORKBENCH_OMP':str(self.bin/'omp'),'XDG_STATE_HOME':str(self.root/'state'),
            'XDG_RUNTIME_DIR':str(self.root/'runtime'),'PYTHONDONTWRITEBYTECODE':'1'}

    def inventory(self):
        return {str(p.relative_to(self.root)):(p.lstat().st_mode,os.readlink(p) if p.is_symlink() else
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in self.root.rglob('*')}

    def invoke(self,*args):
        output,error=io.StringIO(),io.StringIO()
        with contextlib.ExitStack() as stack:
            from workbench.backend import doctor
            stack.enter_context(patch.object(doctor,'FENCE_BASE',self.root/'fence',create=True))
            old=Path.cwd();os.chdir(self.cwd);stack.callback(os.chdir,old)
            stack.enter_context(patch.dict(os.environ,self.env,clear=True))
            stack.enter_context(patch.object(sys,'prefix',str(self.prefix)))
            stack.enter_context(patch.object(sys,'executable',str(self.bin/'python')))
            stack.enter_context(patch.object(sys,'argv',[str(self.entry)]))
            stack.enter_context(patch.object(importlib.metadata,'distribution',return_value=importlib.metadata.PathDistribution(self.meta)))
            stack.enter_context(contextlib.redirect_stdout(output));stack.enter_context(contextlib.redirect_stderr(error))
            try:status=cli.main(['doctor',*args])
            except SystemExit as exc:status=exc.code
        return status,output.getvalue(),error.getvalue()

    def test_no_project_still_checks_global_install_without_creating_state(self):
        before=self.inventory()
        status,out,err=self.invoke('--json')
        self.assertEqual(status,0,err)
        report=json.loads(out)
        self.assertEqual(report['schema_version'],1)
        self.assertEqual(report['overall'],'warning')
        checks={item['id']:item for item in report['checks']}
        for name in ('installation','python','uv','omp'):self.assertEqual(checks[name]['status'],'ok',checks)
        self.assertEqual(checks['project']['status'],'warning')
        self.assertIn('18.11.1',checks['omp']['summary'])
        self.assertEqual(err,'')
        self.assertEqual(self.inventory(),before)

    def project(self):
        from workbench.tasks.repository import TaskRepository
        from workbench.storage.work_records.store import RecordStore
        data=self.cwd/'.wb';data.mkdir(mode=0o700)
        repo=TaskRepository(data/'tasks.sqlite3')
        repo.create_task({'title':'PRIVATE_TASK_BODY','goal':'preserve sentinel'},task_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
        repo.close()
        (data/'tasks.sqlite3').chmod(0o600)
        store=RecordStore(data/'records.sqlite3')
        self.assertEqual(store.create(title='private',body='PRIVATE_RECORD_BODY',actor='manager')['status'],'saved')
        return data

    def test_private_project_db_schema_is_checked_without_opening_source_sqlite(self):
        data=self.project();before=self.inventory()
        status,out,err=self.invoke('--data-dir',str(data),'--json')
        self.assertEqual(status,0,err)
        checks={item['id']:item for item in json.loads(out)['checks']}
        for name in ('permissions','db.tasks','db.records'):self.assertIn(name,checks)
        for name in ('permissions','db.tasks','db.records'):self.assertEqual(checks[name]['status'],'ok',checks)
        self.assertNotIn('PRIVATE_TASK_BODY',out+err);self.assertNotIn('PRIVATE_RECORD_BODY',out+err)
        self.assertEqual(self.inventory(),before)

    def test_corrupt_db_is_error_not_unknown_and_original_bytes_remain(self):
        data=self.project();(data/'records.sqlite3').write_bytes(b'PRIVATE_CORRUPT_BODY_NOT_SQLITE')
        before=self.inventory()
        status,out,err=self.invoke('--data-dir',str(data),'--json')
        self.assertEqual(status,1,out+err)
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertEqual(checks['db.records']['status'],'error',out)
        self.assertNotIn('PRIVATE_CORRUPT_BODY',out+err)
        self.assertEqual(self.inventory(),before)

    def test_auth_and_credential_source_are_metadata_only_and_never_disclosed(self):
        secret='PRIVATE_CREDENTIAL_TOKEN'
        auth=self.root/'home/.omp/agent/agent.db';auth.parent.mkdir(parents=True,mode=0o700)
        auth.write_text(secret);auth.chmod(0o600)
        (self.meta/'direct_url.json').write_text(json.dumps({'url':'https://user:'+secret+'@git.invalid/repo?token='+secret,
            'vcs_info':{'vcs':'git','commit_id':'a'*40}}))
        before=self.inventory();original=os.open
        def guarded(path,*args,**kwargs):
            if Path(path)==auth:raise AssertionError('credential contents must not be opened')
            return original(path,*args,**kwargs)
        with patch('os.open',side_effect=guarded):status,out,err=self.invoke('--json')
        self.assertEqual(status,0,out+err)
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertIn('auth',checks)
        self.assertEqual(checks['auth']['status'],'ok',out)
        self.assertNotIn(secret,out+err);self.assertNotIn('https://user',out+err)
        self.assertIn('a'*40,checks['installation']['summary'])
        self.assertEqual(self.inventory(),before)

    def test_live_backend_and_bridge_use_identity_bound_read_only_snapshot(self):
        from workbench.backend import doctor
        from workbench.backend.paths import DataLayout
        data=self.project()
        child=subprocess.Popen([str(self.bin/'python'),'-c','import time;time.sleep(30)'])
        def close():
            if child.poll() is None:child.kill()
            child.wait(timeout=5)
        self.addCleanup(close)
        ticks=int(Path(f'/proc/{child.pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        reference={'pid':child.pid,'start_ticks':ticks}
        identity=SimpleNamespace(state='running',layout=DataLayout(data),record={'processes':{'backend':reference}})
        class Client:
            def __init__(self,path,**kwargs):
                if kwargs.get('expected_process')!=reference:raise AssertionError('unbound backend connection')
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self):
                return {'backend':{'pid':child.pid,'data_dir':str(data)},
                    'bridge':{role:{'connected':True,'pid_matches_pane':True,'session_id':'fixed-'+role}
                        for role in ('manager','worker')},'PRIVATE_BODY':'PRIVATE_BACKEND_BODY'}
        before=self.inventory()
        with patch.object(doctor,'inspect_backend',return_value=identity,create=True),patch.object(doctor,'UiClient',Client,create=True):
            status,out,err=self.invoke('--data-dir',str(data),'--json')
        self.assertEqual(status,0,out+err)
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertIn('backend',checks);self.assertIn('bridge',checks)
        self.assertEqual(checks['backend']['status'],'ok',out);self.assertEqual(checks['bridge']['status'],'ok',out)
        self.assertNotIn('PRIVATE_BACKEND_BODY',out+err)
        self.assertIsNone(child.poll());self.assertEqual(self.inventory(),before)
        with patch.object(doctor,'inspect_backend',return_value=identity),patch.object(doctor,'UiClient',side_effect=OSError('PRIVATE_ENDPOINT_ERROR')):
            status,out,err=self.invoke('--data-dir',str(data),'--json')
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertEqual(status,1);self.assertEqual(checks['backend']['status'],'unknown');self.assertEqual(checks['bridge']['status'],'unknown')
        self.assertNotIn('PRIVATE_ENDPOINT_ERROR',out+err)
        from workbench.backend.process_identity import BackendIdentityError
        with patch.object(doctor,'inspect_backend',side_effect=BackendIdentityError('process_mismatch',data)):
            status,out,err=self.invoke('--data-dir',str(data),'--json')
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertEqual(status,1);self.assertEqual(checks['backend']['status'],'error');self.assertEqual(checks['bridge']['status'],'unknown')
        self.assertIsNone(child.poll());self.assertEqual(self.inventory(),before)

    def test_active_update_partial_backup_and_resume_are_reported_without_repair(self):
        data=self.project();run=self.root/'state/omp-workbench/updates/run-a';run.mkdir(parents=True,mode=0o700)
        run.parent.chmod(0o700)
        manifest={'schema_version':1,'previous':'a'*40,'target':'b'*40,'backup_complete':False,
            'complete':False,'installation':'unknown','projects':[],'source':'https://user:PRIVATE_BACKUP_CREDENTIAL@invalid/repo'}
        (run/'manifest.json').write_text(json.dumps(manifest));(run/'manifest.json').chmod(0o600)
        (data/'update-resume.json').write_text(json.dumps({'schema_version':1,'backup':str(run),'target':'b'*40}))
        (data/'update-resume.json').chmod(0o600)
        fence=self.root/'fence';fence.mkdir(mode=0o700)
        descriptor=os.open(fence/'update.lock',os.O_CREAT|os.O_RDWR,0o600);self.addCleanup(os.close,descriptor)
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before=self.inventory()
        status,out,err=self.invoke('--data-dir',str(data),'--json')
        self.assertEqual(status,1,out+err)
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertIn('update',checks);self.assertIn('backup',checks);self.assertIn('resume',checks)
        self.assertEqual(checks['update']['status'],'warning',out)
        self.assertEqual(checks['backup']['status'],'error',out)
        self.assertEqual(checks['resume']['status'],'warning',out)
        self.assertNotIn('PRIVATE_BACKUP_CREDENTIAL',out+err)
        self.assertEqual(self.inventory(),before)

    def test_invalid_metadata_and_external_version_output_never_echo_raw_values(self):
        secret='PRIVATE_VERSION_CREDENTIAL'
        (self.meta/'METADATA').write_text('Name: workbench\nVersion: '+secret+'\n')
        (self.bin/'uv').write_text('#!/bin/sh\nprintf "%s" '+repr(secret+'x'*5000)+'\nprintf "%s" '+repr(secret)+' >&2\n')
        before=self.inventory()
        status,out,err=self.invoke('--json')
        self.assertEqual(status,1,out+err)
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertEqual(checks['installation']['status'],'unknown',out)
        self.assertEqual(checks['uv']['status'],'error',out)
        self.assertNotIn(secret,out+err)
        self.assertEqual(self.inventory(),before)

    def test_atomic_manifest_replacement_returns_unknown_json_without_other_writes(self):
        run=self.root/'state/omp-workbench/updates/run-race';run.mkdir(parents=True,mode=0o700)
        run.parent.chmod(0o700)
        manifest=run/'manifest.json'
        contents=json.dumps({'schema_version':1,'complete':True,'backup_complete':True,
            'installation':'verified','projects':[]}).encode()
        manifest.write_bytes(contents);manifest.chmod(0o600)
        scratch=tempfile.TemporaryDirectory(prefix='doctor-manifest-replacement-')
        self.addCleanup(scratch.cleanup)
        replacement=Path(scratch.name)/'manifest.json'
        replacement.write_bytes(contents);replacement.chmod(0o600)
        identity=lambda info:(info.st_dev,info.st_ino)
        original_identity=identity(manifest.stat())
        before=self.inventory();original_read=os.read
        def concurrent_read(descriptor,size):
            value=original_read(descriptor,size)
            if identity(os.fstat(descriptor))==original_identity and replacement.exists():
                os.replace(replacement,manifest)
            return value
        # Control only concurrent producer timing; the read and inode replacement
        # are real, through the public CLI's ordinary manifest validation path.
        with patch('os.read',side_effect=concurrent_read):status,out,err=self.invoke('--json')
        self.assertNotEqual(identity(manifest.stat()),original_identity)
        self.assertEqual(status,1,out+err)
        report=json.loads(out)
        self.assertEqual(report['schema_version'],1)
        self.assertEqual(report['overall'],'error')
        checks={item['id']:item for item in report['checks']}
        self.assertEqual(checks['backup']['status'],'unknown',out)
        self.assertEqual(err,'')
        # The producer replaced identical bytes/mode; doctor adds no other writes.
        self.assertEqual(self.inventory(),before)

    def test_live_wal_image_is_checked_without_changing_source_family(self):
        data=self.project();path=data/'tasks.sqlite3'
        writer=sqlite3.connect(path);self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL');writer.execute('PRAGMA user_version=2');writer.commit()
        writer.execute('PRAGMA user_version=3');writer.commit()
        for name in ('-wal','-shm'):Path(str(path)+name).chmod(0o600)
        before=self.inventory()
        status,out,err=self.invoke('--data-dir',str(data),'--json')
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertEqual(status,0,out+err);self.assertEqual(checks['db.tasks']['status'],'ok',out)
        self.assertEqual(self.inventory(),before)
        # Our own subsequent SQLite read may update SHM reader metadata; it is
        # outside doctor's before/after inventory, not a doctor mutation.
        self.assertEqual(writer.execute('PRAGMA user_version').fetchone(),(3,))

    def test_missing_selected_omp_is_failure_without_fallback_or_writes(self):
        (self.bin/'omp').unlink();before=self.inventory()
        status,out,err=self.invoke('--json')
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertEqual(status,1,out+err);self.assertEqual(checks['omp']['status'],'error')
        self.assertEqual(self.inventory(),before)

    def test_text_json_meanings_match_and_unsupported_fix_is_parse_error(self):
        status,out,err=self.invoke('--json');text_status,text,text_error=self.invoke()
        expected={'installation':'ok','python':'ok','uv':'ok','omp':'ok','auth':'warning','update':'ok','backup':'ok','project':'warning'}
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertEqual(status,0);self.assertEqual(text_status,0);self.assertEqual(err+text_error,'')
        for name,expected_status in expected.items():
            self.assertEqual(checks[name]['status'],expected_status)
            self.assertIn('['+expected_status+'] '+name+':',text)
            self.assertTrue(checks[name]['summary']);self.assertTrue(checks[name]['hint'])
        status,out,err=self.invoke('--fix')
        self.assertEqual(status,2);self.assertEqual(out,'')

    def test_installed_shell_launcher_module_entrypoint_is_recognized(self):
        # The real wb shell launcher execs its own Python -I -m workbench,
        # so Python argv[0] is the distribution's __main__.py, not bin/wb.
        module=self.meta.parent/'workbench/__main__.py';module.parent.mkdir()
        module.write_text('# owned installed module entrypoint\n')
        self.entry=module;before=self.inventory()
        status,out,err=self.invoke('--json')
        self.assertEqual(status,0,out+err)
        checks={item['id']:item for item in json.loads(out)['checks']}
        self.assertEqual(checks['installation']['status'],'ok',out)
        self.assertEqual(self.inventory(),before)
