"""Public stopped-data migration with fixed content and source metadata oracles."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


def inventory(root):
    result = {}
    for p in (root, *sorted(root.rglob('*'))):
        info = p.lstat()
        item = {'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'mode': stat.S_IMODE(info.st_mode)}
        if stat.S_ISREG(info.st_mode):
            item['sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            item['link'] = os.readlink(p)
        result[str(p.relative_to(root))] = item
    return result


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wi-migrate-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'old-state'
        self.source.mkdir(mode=0o700)
        self.project = self.base / 'research'
        self.project.mkdir()
        home = self.base / 'home'; home.mkdir(mode=0o700)
        runtime = self.base / 'runtime'; runtime.mkdir(mode=0o700)
        self.env = {'PATH':'/usr/bin:/bin', 'HOME':str(home), 'LANG':'C.UTF-8',
                    'PYTHONPATH':str(ROOT/'src'), 'PYTHONDONTWRITEBYTECODE':'1',
                    'XDG_RUNTIME_DIR':str(runtime)}
        from workbench.tasks.repository import TaskRepository
        repo = TaskRepository(self.source/'tasks.sqlite3')
        repo.create_task({'title':'kept '+str(self.source)}, task_id='task-fixed')
        repo.close()
        (self.source/'tasks.sqlite3').chmod(0o600)
        self.json(self.source/'backend.json', {
            'contract':{'name':'workbench.ui','version':1}, 'phase':'stopped',
            'data_dir':str(self.source), 'project_dir':str(self.project),
            'runtime_root':str(self.source), 'ui_socket':str(self.source/'ui.sock'),
            'bridge_socket':str(self.source/'bridge.sock'), 'uid':os.geteuid(),
            'boot_id':'00000000-0000-4000-8000-000000000001',
            'processes':{name:{'pid':2000000000+i,'start_ticks':17+i,'owner_epoch':1,'role':name}
                for i,name in enumerate(('backend','manager_omp','worker_omp','host_shell'))},
            'survivors':[], 'shutdown':{'verified':True,'left_running':[],
                'omp_children':{'alive':[],'observed':[]},'previous_survivors':[]}})
        (self.source/'backend.lock').touch(mode=0o600)

    def json(self,path,value):
        path.write_text(json.dumps(value));path.chmod(0o600)

    def cli(self,*args):
        return subprocess.run([sys.executable,'-m','workbench',*map(str,args)], cwd=self.project,
            env=self.env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=20)

    def migrate(self):
        return self.cli('migrate','--from',self.source,'--project',self.project)

    def test_markerless_migration_cannot_use_explicit_or_environment_legacy_fallback(self):
        self.assertEqual(self.cli('status','--json','--data-dir',self.source).returncode,3)
        for phase in ('copying','failed'):
            self.json(self.source/'migration.json',{'schema_version':1,'state':phase,
                'source':'/fixed/original','target':str(self.source)})
            before=inventory(self.source)
            for selector in ('explicit','environment'):
                with self.subTest(phase=phase,selector=selector):
                    arguments=['--data-dir',str(self.source)] if selector=='explicit' else []
                    if selector=='environment':self.env['WORKBENCH_DATA_DIR']=str(self.source)
                    try:
                        for action in (('status','--json'),('start','--no-attach')):
                            result=self.cli(*action,*arguments)
                            self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                            self.assertIn('marker',result.stderr)
                            self.assertEqual(inventory(self.source),before)
                    finally:self.env.pop('WORKBENCH_DATA_DIR',None)

    def test_sqlite_recovery_does_not_open_any_filename(self):
        from unittest.mock import patch
        from workbench.backend.cli import main
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        victim=agent/'history.db'
        with sqlite3.connect(victim) as db:
            db.execute('CREATE TABLE fixed_victim(value TEXT)')
            db.execute("INSERT INTO fixed_victim VALUES ('SOURCE_HISTORY_MUST_NOT_CHANGE')")
        victim.chmod(0o600)
        before=inventory(self.source);connect=sqlite3.connect;calls=[]
        def memory_only(path,*args,**kwargs):
            self.assertEqual(path,':memory:','migration may not give SQLite a filesystem path')
            calls.append(path)
            return connect(path,*args,**kwargs)
        with patch.dict(os.environ,self.env,clear=True),patch('sqlite3.connect',side_effect=memory_only):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertTrue(calls,'fixture must reach real memory SQLite verification')
        self.assertEqual(result,1)
        self.assertEqual(inventory(self.source),before,'SQLite must never modify the source through target aliases')

    def test_database_publication_leaf_replacement_writes_only_the_retained_output_fd(self):
        from unittest.mock import patch
        from workbench.backend.cli import main
        victim=self.source/'tasks.sqlite3';target=self.project/'.wb';write=os.write;events=[]
        before=inventory(self.source)
        def actor(fd,data):
            if os.readlink(f'/proc/self/fd/{fd}')==str(target/'tasks.sqlite3') and not events:
                (target/'tasks.sqlite3').unlink();(target/'tasks.sqlite3').symlink_to(victim)
                events.append('public output leaf replaced before retained FD write')
            return write(fd,data)
        with patch.dict(os.environ,self.env,clear=True),patch('os.write',side_effect=actor):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertEqual(events,['public output leaf replaced before retained FD write'])
        self.assertEqual(result,1)
        self.assertEqual(inventory(self.source),before)
        self.assertEqual(os.readlink(target/'tasks.sqlite3'),str(victim))
        self.assertFalse((target/'project.json').exists())

    def test_database_publication_parent_replacement_cannot_redirect_to_original_history(self):
        from unittest.mock import patch
        from workbench.backend.cli import main
        from workbench.storage.important_log import ImportantLogCatalog
        (self.source/'important-logs').mkdir(mode=0o700)
        catalog=ImportantLogCatalog(self.source/'important-logs',create=True)
        subject={'schemaVersion':1,'kind':'command','id':'12121212-1212-4212-8212-121212121212'}
        self.assertEqual(catalog.append(subject,'event',[],{'schemaVersion':1,'facts':['KEEP_ORIGINAL'],
            'interpretations':[],'unknowns':[]})['state'],'ok')
        target=self.project/'.wb';write=os.write;events=[];before=inventory(self.source)
        def actor(fd,data):
            path=os.readlink(f'/proc/self/fd/{fd}')
            if path.startswith(str(target/'important-logs')+'/') and path.endswith('.sqlite3') and not events:
                (target/'important-logs').rename(target/'important-logs-displaced')
                (target/'important-logs').symlink_to(self.source/'important-logs',target_is_directory=True)
                events.append('important parent replaced before retained output FD write')
            return write(fd,data)
        with patch.dict(os.environ,self.env,clear=True),patch('os.write',side_effect=actor):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertEqual(events,['important parent replaced before retained output FD write'])
        self.assertEqual(result,1)
        self.assertEqual(inventory(self.source),before)
        self.assertEqual(os.readlink(target/'important-logs'),str(self.source/'important-logs'))
        self.assertFalse((target/'project.json').exists())

    def test_nonempty_rollback_journal_is_explicitly_unsupported_without_source_writes(self):
        (self.source/'tasks.sqlite3-journal').write_bytes(b'OWNED_NONEMPTY_ROLLBACK_JOURNAL')
        before=inventory(self.source);result=self.migrate()
        self.assertEqual(result.returncode,1)
        self.assertIn('rollback journal is unsupported',result.stderr)
        self.assertEqual(inventory(self.source),before)
        self.assertFalse((self.project/'.wb/project.json').exists())

    def test_orphan_wal_cannot_be_silently_omitted_from_completed_history(self):
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        (agent/'history.db-wal').write_bytes(b'OWNED_ORPHAN_WAL_NEEDS_ITS_MAIN_DATABASE')
        before=inventory(self.source);result=self.migrate()
        self.assertEqual(result.returncode,1,result.stdout)
        self.assertIn('missing main database',result.stderr)
        self.assertEqual(inventory(self.source),before)
        self.assertFalse((self.project/'.wb/project.json').exists())

    def test_target_without_anonymous_snapshot_support_fails_closed_without_named_fallback(self):
        from unittest.mock import patch
        from workbench.backend.cli import main
        import errno
        opened=os.open;attempts=[];before=inventory(self.source)
        def unsupported(path,flags,*args,**kwargs):
            if flags&os.O_TMPFILE==os.O_TMPFILE:
                attempts.append(os.fspath(path));raise OSError(errno.EOPNOTSUPP,'fixture unsupported filesystem')
            return opened(path,flags,*args,**kwargs)
        with patch.dict(os.environ,self.env,clear=True),patch('os.open',side_effect=unsupported):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertEqual(attempts,['.'])
        self.assertEqual(result,1)
        self.assertEqual(inventory(self.source),before)
        target=self.project/'.wb'
        self.assertEqual(set(p.name for p in target.iterdir()),{'migration.json'})
        self.assertEqual(json.loads((target/'migration.json').read_text())['state'],'failed')

    def test_memory_failure_reports_phase_and_error_type_without_publishing_project(self):
        from unittest.mock import patch
        from contextlib import redirect_stderr
        from io import StringIO
        from workbench.backend.cli import main
        before=inventory(self.source);output=StringIO()
        with patch.dict(os.environ,self.env,clear=True),redirect_stderr(output),patch(
                'workbench.backend.migration_sqlite.bytearray',side_effect=MemoryError,create=True):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertEqual(result,1)
        self.assertIn('MemoryError',output.getvalue())
        self.assertIn('source_database_snapshot',output.getvalue())
        self.assertEqual(inventory(self.source),before)
        self.assertFalse((self.project/'.wb/project.json').exists())

    def test_credential_free_custom_models_stay_active_with_exact_bytes(self):
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        content=(b'startup:\n  setupWizard: false\nproviders:\n  independent-provider:\n'
                 b'    baseUrl: https://models.example.invalid/v1\n    api: openai-completions\n'
                 b'    auth: none\n    models:\n      - id: fixed-model\n'
                 b'        name: Independent model\n        contextWindow: 4096\n        maxTokens: 512\n')
        (agent/'models.yml').write_bytes(content)
        before=inventory(self.source)
        result=self.migrate();self.assertEqual(result.returncode,0,result.stderr)
        target=self.project/'.wb'
        self.assertEqual((target/'omp-root/agent/models.yml').read_bytes(),content)
        self.assertFalse((target/'migration-history/omp-root/agent/models.yml').exists())
        self.assertEqual(inventory(self.source),before)

    def test_credential_model_config_is_refused_before_copy_or_archive_without_value_in_error(self):
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        (agent/'models.yml').write_bytes(b'providers:\n  other-provider:\n'
            b'    baseUrl: https://models.example.invalid/v1\n    api: openai-completions\n'
            b'    auth: none\n    apiKey: PRIVATE_FIXTURE_CREDENTIAL_NEVER_COPY\n'
            b'    models:\n      - id: fixed-model\n')
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertNotIn('PRIVATE_FIXTURE_CREDENTIAL_NEVER_COPY',result.stdout+result.stderr)
        self.assertIn('models.yml',result.stderr)
        self.assertFalse((self.project/'.wb').exists())
        self.assertEqual(inventory(self.source),before)

    def test_unknown_omp_config_is_not_opaquely_archived(self):
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        (agent/'config.yml').write_bytes(b'apiKey: PRIVATE_CONFIG_VALUE_NEVER_COPY\n')
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertIn('config.yml',result.stderr)
        self.assertNotIn('PRIVATE_CONFIG_VALUE_NEVER_COPY',result.stdout+result.stderr)
        self.assertFalse((self.project/'.wb').exists())
        self.assertEqual(inventory(self.source),before)

    def test_unsupported_model_syntax_and_fields_fail_closed_before_any_target_copy(self):
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        valid=(b'providers:\n  distinct-provider:\n    baseUrl: https://models.example.invalid/v1\n'
               b'    api: openai-completions\n    auth: none\n    models:\n      - id: one\n')
        variants={
            'duplicate':valid.replace(b'    auth: none\n',b'    auth: none\n    auth: none\n'),
            'headers':valid.replace(b'    auth: none\n',b'    auth: none\n    headers: PRIVATE_NEVER_ECHO\n'),
            'alias':valid.replace(b'    auth: none',b'    auth: *PRIVATE_NEVER_ECHO'),
            'tag':valid.replace(b'    auth: none',b'    auth: !!str none'),
            'merge':valid.replace(b'    auth: none',b'    <<: PRIVATE_NEVER_ECHO\n    auth: none'),
            'url-userinfo':valid.replace(b'https://models.example.invalid/v1',b'https://PRIVATE_NEVER_ECHO@models.example.invalid/v1'),
            'url-query':valid.replace(b'https://models.example.invalid/v1',b'https://models.example.invalid/v1?key=PRIVATE_NEVER_ECHO'),
            'wrong-type':valid.replace(b'    auth: none',b'    auth: false'),
            'unconsumed':valid+b'---\nextra: PRIVATE_NEVER_ECHO\n',
        }
        for name,content in variants.items():
            with self.subTest(name=name):
                (agent/'models.yml').write_bytes(content);before=inventory(self.source)
                result=self.migrate()
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertNotIn('PRIVATE_NEVER_ECHO',result.stdout+result.stderr)
                self.assertFalse((self.project/'.wb').exists())
                self.assertEqual(inventory(self.source),before)

    def test_stopped_copy_reopens_fixed_task_and_keeps_every_source_byte_and_mtime(self):
        sessions=self.source/'omp-root/agent/sessions';sessions.mkdir(parents=True,mode=0o700)
        (sessions/'fixed.jsonl').write_bytes(b'{"id":"session-fixed","message":"old path remains"}\n')
        before=inventory(self.source)
        result=self.migrate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        target=self.project/'.wb'
        marker=json.loads((target/'project.json').read_text())
        self.assertEqual(marker['state'],'completed')
        self.assertEqual(marker['project_root'],str(self.project))
        self.assertEqual(inventory(self.source),before)
        from workbench.tasks.repository import TaskRepository
        repo=TaskRepository(target/'tasks.sqlite3')
        try:self.assertEqual(repo.get_task_spec('task-fixed',1)['spec'],{'title':'kept '+str(self.source)})
        finally:repo.close()
        self.assertEqual((target/'omp-root/agent/sessions/fixed.jsonl').read_bytes(),b'{"id":"session-fixed","message":"old path remains"}\n')
        self.assertFalse((target/'backend.json').exists())
        self.assertFalse((target/'backend-starting.json').exists())
        self.assertFalse((target/'ui.sock').exists())

    def test_missing_related_writer_identity_refuses_without_target_creation(self):
        record=json.loads((self.source/'backend.json').read_text())
        del record['processes']['host_shell']
        self.json(self.source/'backend.json',record)
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('writer',result.stderr)
        self.assertFalse((self.project/'.wb').exists())
        self.assertEqual(inventory(self.source),before)

    def test_ended_backend_does_not_authorize_copy_while_admission_parent_is_alive(self):
        from workbench.backend.boot import read_boot_id
        ticks=int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        info=(self.source/'backend.lock').stat()
        self.json(self.source/'backend-starting.json',{'schema_version':1,'startup_id':'a'*32,
            'data_dir':str(self.source),'project_dir':str(self.project),'project_id':None,
            'runtime_root':str(self.source),'uid':os.geteuid(),'boot_id':read_boot_id(),
            'frontend':{'pid':os.getpid(),'start_ticks':ticks},
            'backend':{'pid':2000000000,'start_ticks':17},
            'lock':{'device':info.st_dev,'inode':info.st_ino},'previous_record_sha256':None})
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('frontend',result.stderr)
        self.assertFalse((self.project/'.wb').exists())
        self.assertEqual(inventory(self.source),before)

    def test_live_or_unknown_survivor_never_becomes_permission_to_copy(self):
        from workbench.backend.boot import read_boot_id
        ticks=int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        record=json.loads((self.source/'backend.json').read_text())
        record['survivors']=[{'name':'supervisor','pid':os.getpid(),'start_ticks':ticks,
                              'boot_id':read_boot_id()}]
        self.json(self.source/'backend.json',record)
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('survivor',result.stderr)
        self.assertFalse((self.project/'.wb').exists())
        self.assertEqual(inventory(self.source),before)

    def test_unrecognized_database_table_retains_incomplete_target_and_blocks_discovery(self):
        connection=sqlite3.connect(self.source/'tasks.sqlite3')
        connection.execute('CREATE TABLE future_schema(value TEXT)');connection.close()
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('schema',result.stderr)
        self.assertFalse((self.project/'.wb/project.json').exists())
        self.assertEqual(json.loads((self.project/'.wb/migration.json').read_text())['state'],'failed')
        self.assertEqual(inventory(self.source),before)
        result=self.cli('status','--json')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('marker',result.stderr)

    def test_raw_public_readers_reopen_rebound_storage_without_rewriting_history(self):
        import base64
        from workbench.storage.log_raw.store import RawLogStore
        from workbench.storage.log_raw.port import raw_query
        run='11111111-1111-4111-8111-111111111111'
        payload=b'RAW-fixed '+os.fsencode(self.source)+b'\x00\xff'
        with RawLogStore(self.source/'raw-logs') as store:store.append(run,payload)
        directory=self.source/'workflow/runs'/run;directory.mkdir(parents=True,mode=0o700)
        command='printf '+str(self.source)+' historical command'
        self.json(directory/'run.json',{'run_id':run,'raw_log':str(self.source/'raw-logs'/f'{run}.log'),
            'result':str(directory/'run.json'),'command':command,'cwd':'/missing/external-worktree',
            'shell_state':'ended','exit_confirmed':True})
        before=inventory(self.source)
        result=self.migrate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        target=self.project/'.wb'
        self.assertEqual(inventory(self.source),before)
        reopened=raw_query(target,'run',run)
        self.assertEqual(base64.b64decode(reopened['read']['dataBase64']),payload)
        with RawLogStore(target/'raw-logs') as store:
            self.assertEqual(store.status(run).stored_bytes,len(payload))
            self.assertIsNone(store.status(run).storage_error)
        record=json.loads((target/'workflow/runs'/run/'run.json').read_text())
        self.assertEqual(record['command'],command)
        self.assertEqual(record['cwd'],'/missing/external-worktree')
        self.assertEqual(record['result'],str(target/'workflow/runs'/run/'run.json'))
        manifest=json.loads((target/'migration-manifest.json').read_text())
        self.assertIn('/missing/external-worktree',str(manifest['source_dependencies']))

    def test_auth_link_is_classified_by_metadata_and_recreated_without_credential_copy(self):
        store=Path(self.env['HOME'])/'.omp/agent/agent.db';store.parent.mkdir(parents=True)
        credential=b'PRIVATE-CREDENTIAL-MUST-NOT-BE-COPIED'
        store.write_bytes(credential);store.chmod(0o600)
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        (agent/'agent.db').symlink_to(store)
        before=inventory(self.source)
        result=self.migrate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        target=self.project/'.wb'
        self.assertEqual(os.readlink(target/'omp-root/agent/agent.db'),str(store))
        self.assertEqual(inventory(self.source),before)
        for p in target.rglob('*'):
            if p.is_file() and not p.is_symlink():self.assertNotIn(credential,p.read_bytes())
        row=json.loads((target/'migration-manifest.json').read_text())['inventory']['omp-root/agent/agent.db']
        self.assertEqual(row['classification'],'auth-link-metadata-only')
        self.assertNotIn('sha256',row)

    def test_wal_and_important_rows_are_recovered_only_on_target_and_publicly_reopened(self):
        from workbench.storage.important_log import ImportantLogCatalog,ImportantLogReader
        subject={'schemaVersion':1,'kind':'command','id':'22222222-2222-4222-8222-222222222222'}
        body={'schemaVersion':1,'facts':['IMPORTANT fixed '+str(self.source)],'interpretations':[],'unknowns':[]}
        catalog=ImportantLogCatalog(self.source/'important-logs',create=True)
        self.assertEqual(catalog.append(subject,'event',[],body)['state'],'ok')
        code="""import os,sys
from workbench.tasks.repository import TaskRepository
r=TaskRepository(sys.argv[1]);r._connection.execute('PRAGMA journal_mode=WAL');r._connection.execute('PRAGMA wal_autocheckpoint=0')
r.create_task({'title':'committed-only-in-WAL'},task_id='wal-task-fixed');os._exit(0)
"""
        subprocess.run([sys.executable,'-c',code,str(self.source/'tasks.sqlite3')],env=self.env,check=True,timeout=15)
        self.assertGreater((self.source/'tasks.sqlite3-wal').stat().st_size,0)
        before=inventory(self.source)
        from unittest.mock import patch
        from workbench.backend.cli import main
        connect=sqlite3.connect
        def target_only(database,*args,**kwargs):
            self.assertFalse(str(database).startswith(str(self.source)), 'source SQLite connection is forbidden')
            return connect(database,*args,**kwargs)
        with patch.dict(os.environ,self.env,clear=True),patch('sqlite3.connect',side_effect=target_only):
            status=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertEqual(status,0)
        self.assertEqual(inventory(self.source),before)
        from workbench.tasks.repository import TaskRepository
        repo=TaskRepository(self.project/'.wb/tasks.sqlite3')
        try:self.assertEqual(repo.get_task_spec('wal-task-fixed',1)['spec'],{'title':'committed-only-in-WAL'})
        finally:repo.close()
        result=ImportantLogReader(self.project/'.wb/important-logs').show(subject)
        self.assertTrue(result['complete'],result)
        self.assertEqual([e['entry']['body'] for e in result['entries']],[body])

    def test_cache_assets_and_internal_git_worktree_are_explicit_noncopy_inventory(self):
        cache=self.source/'omp-root/cache';cache.mkdir(parents=True,mode=0o700)
        (cache/'regenerable.bin').write_bytes(b'CACHE only')
        agents=self.source/'omp-root/agent/agents';agents.mkdir(parents=True,mode=0o700)
        from workbench.backend.omp_home import default_agents_dir
        (agents/'explorer.md').symlink_to(default_agents_dir()/'explorer.md')
        worktree=self.source/'workflow/worktrees/original';worktree.mkdir(parents=True,mode=0o700)
        (worktree/'.git').write_text('gitdir: /external/git/worktrees/original\n')
        (worktree/'result.txt').write_bytes(b'RESULT must stay at original worktree')
        before=inventory(self.source)
        result=self.migrate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        target=self.project/'.wb'
        self.assertFalse((target/'omp-root/cache').exists())
        self.assertFalse((target/'omp-root/agent/agents/explorer.md').exists())
        self.assertFalse((target/'workflow/worktrees/original').exists())
        self.assertEqual(inventory(self.source),before)
        manifest=json.loads((target/'migration-manifest.json').read_text())
        self.assertEqual(set(manifest['inventory']),set(before)-{'.'})
        self.assertEqual(manifest['inventory']['omp-root/cache/regenerable.bin']['classification'],'regenerable-cache')
        self.assertEqual(manifest['inventory']['omp-root/agent/agents/explorer.md']['classification'],'installed-asset')
        self.assertIn(str(worktree),str(manifest['source_dependencies']))

    def test_migration_archives_prior_boot_acceptance_and_requires_fresh_recovery_confirmation(self):
        from workbench.backend.boot import BootStore,read_boot_id
        old={'version':1,'recorded_boot_id':'00000000-0000-4000-8000-000000000001',
            'confirmed_boot_id':'00000000-0000-4000-8000-000000000001','pending':False,
            'pending_boot_id':None,'reason':None,'history':[]}
        self.json(self.source/'boot.json',old)
        self.json(self.source/'raw-rotation.json',{'enabled':True,'old_acceptance':'not-current'})
        compatibility=self.source/'summary-compatibility';compatibility.mkdir(mode=0o700)
        self.json(compatibility/'old-pass.json',{'passed':True,'source_dir':str(self.source)})
        before=inventory(self.source)
        result=self.migrate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        target=self.project/'.wb'
        self.assertFalse((target/'backend.json').exists())
        self.assertFalse((target/'raw-rotation.json').exists())
        self.assertFalse((target/'summary-compatibility').exists())
        self.assertEqual(json.loads((target/'migration-history/boot.json').read_text()),old)
        state=BootStore(target/'boot.json').begin(None)
        self.assertTrue(state.pending, 'migration must not turn prior history into fresh acceptance')
        self.assertEqual(state.reason,'reboot')
        self.assertIsNone(state.confirmed)
        self.assertEqual(state.current,read_boot_id())
        self.assertEqual(inventory(self.source),before)

    def test_segmented_retained_bytes_reopen_without_copying_rotation_activation(self):
        import base64
        from workbench.storage.log_raw import SegmentedRawStore
        from workbench.storage.log_raw.port import DisabledSegmentedRawPort,raw_query
        task='33333333-3333-4333-8333-333333333333';run='44444444-4444-4444-8444-444444444444'
        directory=self.source/'workflow/runs'/run;directory.mkdir(parents=True,mode=0o700)
        with SegmentedRawStore(self.source/'raw-v2',segment_limit=2048,segment_count=2) as store:
            port=DisabledSegmentedRawPort(store)
            path,descriptor=port.prepare_task(directory,task,1,run)
            port.append(descriptor,b'SEGMENT-fixed\x00bytes')
            self.json(directory/'run.json',{'run_id':run,'log_format':'segmented-v1',
                'raw_log_ref':descriptor['raw_log_ref'],'raw_source':descriptor['source']})
        before=inventory(self.source)
        result=self.migrate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(inventory(self.source),before)
        reopened=raw_query(self.project/'.wb','run',run)
        self.assertEqual(b''.join(base64.b64decode(c['dataBase64']) for c in reopened['read']['chunks']),b'SEGMENT-fixed\x00bytes')
        self.assertFalse((self.project/'.wb/raw-rotation.json').exists())

    def test_source_project_ancestor_and_target_use_one_common_lock_set(self):
        record=json.loads((self.source/'backend.json').read_text())
        record['project_dir']=str(self.base)
        self.json(self.source/'backend.json',record)
        before=inventory(self.source)
        result=self.migrate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(inventory(self.source),before)

    def test_source_boot_facts_preserve_clean_pending_previous_and_unreadable_core_boundaries(self):
        from workbench.backend.boot import BootStore,read_boot_id
        from workbench.app.recovery import StartupReconciler
        from workbench.backend.paths import DataLayout
        current=read_boot_id();old='00000000-0000-4000-8000-000000000001'
        record=json.loads((self.source/'backend.json').read_text())
        for name,boot,pending,reason,expected in (
                ('clean',current,False,None,(False,None)),
                ('pending',current,True,'boot_marker_unknown',(True,'boot_marker_unknown')),
                ('previous',old,False,None,(True,'reboot')),
                ('unreadable',current,False,None,(True,'boot_record_unreadable'))):
            with self.subTest(name=name):
                self.project=self.base/name;self.project.mkdir()
                record['boot_id']=boot;record['project_dir']=str(self.project)
                self.json(self.source/'backend.json',record)
                self.json(self.source/'boot.json',{'version':1,'recorded_boot_id':boot,
                    'confirmed_boot_id':boot,'pending':pending,'pending_boot_id':boot if pending else None,
                    'reason':reason,'history':[]})
                if name=='unreadable':(self.source/'boot.json').write_bytes(b'invalid-boot-record')
                before=inventory(self.source)
                result=self.migrate();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                target=self.project/'.wb';self.assertEqual(inventory(self.source),before)
                fresh=json.loads((target/'boot.json').read_text())
                self.assertIsNone(fresh['confirmed_boot_id'])
                reopened=BootStore(target/'boot.json').begin(None)
                self.assertEqual((reopened.pending,reopened.reason),expected)
                report=StartupReconciler(DataLayout(target),BootStore(target/'boot.json'),
                    handoff_journal=target/'workflow/handoffs.jsonl').run()
                self.assertEqual(report.report['classification'],'boot_unknown')
                self.assertEqual((report.boot.pending,report.boot.reason),expected)
                self.assertIsNone(report.previous)
                self.assertFalse((target/'backend.json').exists())

    def test_completion_is_not_claimed_after_root_swap_and_never_overwrites_other_marker(self):
        from unittest.mock import patch
        from workbench.backend.cli import main
        from workbench.backend.project import read_project_marker
        from workbench.backend.paths import DataDirError
        other=self.base/'other-data';other.mkdir(mode=0o700)
        self.json(other/'project.json',{'existing':'MUST REMAIN'})
        marker=(other/'project.json').read_bytes()
        target=self.project/'.wb';moved=self.project/'.wb-interrupted'
        link=os.link
        def swap(src,dst,*args,**kwargs):
            if dst=='project.json':
                target.rename(moved);target.symlink_to(other,target_is_directory=True)
            return link(src,dst,*args,**kwargs)
        before=inventory(self.source)
        with patch.dict(os.environ,self.env,clear=True),patch('os.link',side_effect=swap):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertNotEqual(result,0)
        self.assertEqual((other/'project.json').read_bytes(),marker)
        self.assertEqual(inventory(self.source),before)
        with self.assertRaises(DataDirError):read_project_marker(target)
        with self.assertRaises(DataDirError):read_project_marker(moved)

    def test_lifecycle_run_target_alive_is_a_writer_even_after_backend_clean_stop(self):
        from dataclasses import asdict
        from workbench.app.lifecycle import LifecycleRecord,PeerRef,ControlState
        from workbench.runtime.process_evidence import ProcessRef
        from workbench.backend.boot import read_boot_id
        ticks=int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        life=LifecycleRecord('task-fixed',1,'run-fixed','approval-fixed',1,read_boot_id(),None,
            PeerRef('manager','manager-old',1,ProcessRef('manager_omp',2000000001,18,1)),
            PeerRef('worker','worker-old',1,ProcessRef('worker_omp',2000000002,19,1)),
            ProcessRef('host_shell',2000000003,20,1),
            (ProcessRef('run_target',os.getpid(),ticks,1),),
            ControlState('manager',1,'control_wait',False,False))
        self.json(self.source/'lifecycle.json',{'version':1,'record':asdict(life)})
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('run_target',result.stderr)
        self.assertFalse((self.project/'.wb').exists())
        self.assertEqual(inventory(self.source),before)

    def test_active_task_child_is_proved_on_target_snapshot_before_completion(self):
        from workbench.tasks.repository import TaskRepository
        from workbench.backend.boot import read_boot_id
        record=json.loads((self.source/'backend.json').read_text());record['boot_id']=read_boot_id()
        self.json(self.source/'backend.json',record)
        repo=TaskRepository(self.source/'tasks.sqlite3')
        repo.set_session_settings({'session':'fixed'})
        repo.approve_scope('task-fixed',1,{'command':'keep original'})
        repo.proceed('task-fixed',1,'fixed proceed')
        run=repo.start_run('task-fixed',1,inputs={'worktree_path':'/external/original'})
        ticks=int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        repo.record_shell_event(run,'started',{'child_pid':os.getpid(),'child_start_ticks':ticks})
        repo.close()
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('run_target',result.stderr)
        self.assertEqual(inventory(self.source),before)
        self.assertFalse((self.project/'.wb/project.json').exists())

    def test_reservation_requires_the_supplied_open_file_description_not_another_same_pid_lock(self):
        import fcntl
        from unittest.mock import patch
        from workbench.backend.paths import InstanceLock,DataDirError
        legitimate=InstanceLock(self.source/'backend.lock')
        candidate=InstanceLock(self.source/'backend.lock')
        before=inventory(self.source)
        legitimate.acquire_existing_readonly()
        try:
            # Only the OS acquisition response is faulted. The kernel really owns
            # legitimate's different fd; candidate's fdinfo must prove its own hold.
            with patch('fcntl.flock',return_value=None):
                with self.assertRaises(DataDirError):candidate.acquire_existing_readonly()
            legitimate.verify_reservation(self.source/'backend.lock')
        finally:
            candidate.release();legitimate.release()
        self.assertEqual(inventory(self.source),before)

    def test_corrupt_important_body_checksum_cannot_complete_a_migration(self):
        from workbench.storage.important_log import ImportantLogCatalog
        catalog=ImportantLogCatalog(self.source/'important-logs',create=True)
        subject={'schemaVersion':1,'kind':'command','id':'55555555-5555-4555-8555-555555555555'}
        body={'schemaVersion':1,'facts':['fixed important body'],'interpretations':[],'unknowns':[]}
        self.assertEqual(catalog.append(subject,'event',[],body)['state'],'ok')
        database=self.source/'important-logs'/f'{catalog.active_uuid()}.sqlite3'
        connection=sqlite3.connect(database);connection.execute("UPDATE entries SET body_sha256=?",('0'*64,));connection.commit();connection.close()
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((self.project/'.wb/project.json').exists())
        self.assertEqual(inventory(self.source),before)
        self.assertIn('checksum',result.stderr)

    def test_important_catalog_missing_mapping_refuses_even_when_all_database_rows_are_valid(self):
        from workbench.storage.important_log import ImportantLogCatalog
        catalog=ImportantLogCatalog(self.source/'important-logs',create=True)
        self.json(self.source/'important-logs/active.json',{'version':1,'generation':1,
            'active':'66666666-6666-4666-8666-666666666666','archives':[]})
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('catalog',result.stderr)
        self.assertFalse((self.project/'.wb/project.json').exists())
        self.assertEqual(inventory(self.source),before)

    def test_corrupt_omp_history_database_is_not_an_opaque_successful_copy(self):
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        (agent/'history.db').write_bytes(b'not a SQLite history database')
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((self.project/'.wb/project.json').exists())
        self.assertEqual(inventory(self.source),before)

    def test_missing_source_has_actionable_refusal_without_traceback_or_target(self):
        result=self.cli('migrate','--from',self.base/'missing','--project',self.project)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('migration',result.stderr)
        self.assertNotIn('Traceback',result.stderr)
        self.assertFalse((self.project/'.wb').exists())

    def test_durable_user_pause_remains_paused_after_copy(self):
        from workbench.app.recovery import PauseStore
        self.json(self.source/'automation.json',{'version':1,'paused':True,'at':17})
        before=inventory(self.source)
        result=self.migrate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(PauseStore(self.project/'.wb/automation.json').load())
        self.assertEqual(inventory(self.source),before)

    def test_shutdown_left_session_is_not_silently_ignored_as_history(self):
        from workbench.backend.boot import read_boot_id
        record=json.loads((self.source/'backend.json').read_text());record['boot_id']=read_boot_id()
        ticks=int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        record['shutdown']['left_running']=[{'pid':os.getpid(),'start_ticks':ticks}]
        self.json(self.source/'backend.json',record)
        before=inventory(self.source)
        result=self.migrate()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('shutdown',result.stderr)
        self.assertFalse((self.project/'.wb').exists())
        self.assertEqual(inventory(self.source),before)

    def test_preflight_existing_empty_symlink_and_nested_targets_never_merge(self):
        before=inventory(self.source)
        target=self.project/'.wb';target.mkdir()
        result=self.migrate();self.assertNotEqual(result.returncode,0);self.assertEqual(list(target.iterdir()),[])
        target.rmdir()
        other=self.base/'other';other.mkdir();(other/'sentinel').write_bytes(b'OTHER')
        target.symlink_to(other,target_is_directory=True)
        result=self.migrate();self.assertNotEqual(result.returncode,0)
        self.assertEqual((other/'sentinel').read_bytes(),b'OTHER');target.unlink()
        result=self.cli('migrate','--from',self.source,'--project',self.source)
        self.assertNotEqual(result.returncode,0);self.assertIn('nested',result.stderr)
        link=self.base/'source-link';link.symlink_to(self.source,target_is_directory=True)
        result=self.cli('migrate','--from',link,'--project',self.project)
        self.assertNotEqual(result.returncode,0);self.assertIn('symlink',result.stderr)
        self.assertFalse(target.exists());self.assertEqual(inventory(self.source),before)

    def test_live_lock_and_missing_lock_refuse_without_creating_or_changing_source_files(self):
        from workbench.backend.paths import InstanceLock
        lock=InstanceLock(self.source/'backend.lock');lock.acquire_existing_readonly()
        before=inventory(self.source)
        try:
            result=self.migrate();self.assertNotEqual(result.returncode,0)
            lock.verify_reservation(self.source/'backend.lock')
            self.assertFalse((self.project/'.wb').exists())
            self.assertEqual(inventory(self.source),before)
        finally:lock.release()
        (self.source/'backend.lock').unlink()
        before=inventory(self.source)
        result=self.migrate();self.assertNotEqual(result.returncode,0)
        self.assertEqual(inventory(self.source),before)
        self.assertFalse((self.source/'backend.lock').exists())
        self.assertFalse((self.project/'.wb').exists())

    def test_unknown_nested_file_and_special_fifo_are_refused_without_being_opened(self):
        agent=self.source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700)
        unknown=agent/'unknown-future.bin';unknown.write_bytes(b'UNRECOGNIZED')
        before=inventory(self.source)
        result=self.migrate();self.assertNotEqual(result.returncode,0)
        self.assertIn('unrecognized',result.stderr);self.assertEqual(inventory(self.source),before)
        unknown.unlink();os.mkfifo(self.source/'unrecognized-fifo',0o600)
        before=inventory(self.source)
        result=self.migrate();self.assertNotEqual(result.returncode,0)
        self.assertEqual(inventory(self.source),before)
        self.assertFalse((self.project/'.wb').exists())

    def test_backend_diagnostic_bytes_are_history_and_previous_record_is_inactive(self):
        data=b'OLD diagnostic '+os.fsencode(self.source)+b' PID 12345\n'
        (self.source/'backend.log').write_bytes(data)
        previous=(self.source/'backend.json').read_bytes()
        (self.source/'backend.prev.json').write_bytes(previous)
        (self.source/'backend.prev.json').chmod(0o600)
        before=inventory(self.source)
        result=self.migrate();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        target=self.project/'.wb'
        self.assertEqual((target/'backend.log').read_bytes(),data)
        self.assertEqual((target/'migration-history/backend.prev.json').read_bytes(),previous)
        self.assertFalse((target/'backend.prev.json').exists())
        self.assertEqual(inventory(self.source),before)

    def test_disk_full_during_db_snapshot_retains_incomplete_target_and_source(self):
        from unittest.mock import patch
        from workbench.backend.cli import main
        import errno
        before=inventory(self.source)
        with patch.dict(os.environ,self.env,clear=True),patch('os.write',side_effect=OSError(errno.ENOSPC,'fixture disk full')):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertEqual(result,1)
        self.assertEqual(inventory(self.source),before)
        self.assertTrue((self.project/'.wb').is_dir())
        self.assertFalse((self.project/'.wb/project.json').exists())
        diagnostic=json.loads((self.project/'.wb/migration.json').read_text())
        self.assertEqual(diagnostic['state'],'failed')
        self.assertEqual(diagnostic.get('phase'),'source_database_snapshot')
        result=self.cli('status','--json');self.assertNotEqual(result.returncode,0)
        self.assertIn('marker',result.stderr)
        self.assertNotEqual(self.migrate().returncode,0,'incomplete target cannot be silently taken over')

    def test_marker_fsync_failure_cannot_leave_a_discoverable_completed_project(self):
        from unittest.mock import patch
        from workbench.backend.cli import main
        from workbench.backend.project import read_project_marker
        from workbench.backend.paths import DataDirError
        import errno
        target=self.project/'.wb';fsync=os.fsync;failed=[]
        def fault(fd):
            if not failed and stat.S_ISDIR(os.fstat(fd).st_mode) and os.readlink(f'/proc/self/fd/{fd}')==str(target) and (target/'project.json').exists():
                failed.append(True);raise OSError(errno.EIO,'fixture completed-marker fsync failed')
            return fsync(fd)
        before=inventory(self.source)
        with patch.dict(os.environ,self.env,clear=True),patch('os.fsync',side_effect=fault):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertTrue(failed);self.assertEqual(result,1)
        self.assertEqual(inventory(self.source),before)
        with self.assertRaises(DataDirError):read_project_marker(target)

    def test_marker_collision_preserves_other_marker_and_failed_migration_stays_undiscoverable(self):
        from unittest.mock import patch
        from workbench.backend.cli import main
        from workbench.backend.project import read_project_marker
        from workbench.backend.paths import DataDirError
        target=self.project/'.wb';link=os.link;existing=[]
        def collide(src,dst,*args,**kwargs):
            if dst=='project.json':
                self.json(target/'project.json',{'schema_version':1,'state':'completed',
                    'project_id':'77777777-7777-4777-8777-777777777777','project_root':str(self.project)})
                existing.append((target/'project.json').read_bytes())
            return link(src,dst,*args,**kwargs)
        before=inventory(self.source)
        with patch.dict(os.environ,self.env,clear=True),patch('os.link',side_effect=collide):
            result=main(['migrate','--from',str(self.source),'--project',str(self.project)])
        self.assertEqual(result,1)
        self.assertEqual((target/'project.json').read_bytes(),existing[0])
        self.assertEqual(inventory(self.source),before)
        with self.assertRaises(DataDirError):read_project_marker(target)

    def test_interrupted_copy_blocks_concurrent_start_and_retains_source_and_incomplete_target(self):
        import signal,time
        ready=self.base/'copy-ready'
        code="""import os,sys,time
from pathlib import Path
source,target,ready=map(Path,sys.argv[1:]);write=os.write
def held(fd,data):
    if os.readlink(f'/proc/self/fd/{fd}').startswith(str(target/'.wb')+'/') and not ready.exists():
        ready.write_text('DB snapshot copy is paused')
        while True:time.sleep(.05)
    return write(fd,data)
os.write=held
from workbench.backend.cli import main
raise SystemExit(main(['migrate','--from',str(source),'--project',str(target)]))
"""
        before=inventory(self.source)
        child=subprocess.Popen([sys.executable,'-c',code,str(self.source),str(self.project),str(ready)],
            cwd=self.project,env=self.env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        pidfd=os.pidfd_open(child.pid)
        ticks=int(Path(f'/proc/{child.pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        try:
            deadline=time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(ready.exists())
            partial=inventory(self.project/'.wb')
            start=self.cli('start','--no-attach','--timeout','0.2')
            self.assertNotEqual(start.returncode,0);self.assertIn('marker',start.stderr)
            self.assertEqual(inventory(self.project/'.wb'),partial)
            self.assertNotEqual(self.migrate().returncode,0)
            self.assertEqual(inventory(self.project/'.wb'),partial)
            actual=int(Path(f'/proc/{child.pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
            self.assertEqual(actual,ticks)
            signal.pidfd_send_signal(pidfd,signal.SIGINT)
            stdout,stderr=child.communicate(timeout=10)
            self.assertEqual(child.returncode,1,(stdout,stderr))
            self.assertNotIn(b'Traceback',stderr)
            self.assertEqual(inventory(self.source),before)
            self.assertFalse((self.project/'.wb/project.json').exists())
            self.assertEqual(json.loads((self.project/'.wb/migration.json').read_text())['state'],'failed')
        finally:
            if child.poll() is None:
                signal.pidfd_send_signal(pidfd,signal.SIGKILL);child.wait(timeout=5)
            os.close(pidfd)

    def test_legacy_text_prediction_cache_is_classified_and_regenerated_not_migrated(self):
        predict=self.source/'omp-root/agent/predict/ngram';predict.mkdir(parents=True,mode=0o700)
        (predict/'cursor.json').write_bytes(b'{"offset":17}')
        (predict/'ngram.snapshot').write_bytes(b'fixed regenerable prediction state')
        before=inventory(self.source)
        result=self.migrate();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(inventory(self.source),before)
        self.assertFalse((self.project/'.wb/omp-root/agent/predict').exists())
        manifest=json.loads((self.project/'.wb/migration-manifest.json').read_text())
        self.assertEqual(manifest['inventory']['omp-root/agent/predict/ngram/ngram.snapshot']['classification'],'regenerable-cache')

if __name__=='__main__':unittest.main()
