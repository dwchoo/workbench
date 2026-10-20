"""Opt-in actual installed read-only doctor; verified native namespace only.

Reuses the installed Git/uv and real OMP startup recipe. No model turn is sent.
All fixture mutations are explicit setup/teardown, outside doctor inventories.
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
import stat
import time
import unittest

from live_workbench_update import UpdateRun, digest, save
from live_project_sessions import InstalledRun, Provider, state

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ('WD02_PRIVATE_TASK', 'WD02_PRIVATE_RECORD', 'WD02_PRIVATE_RAW',
           'WD02_PRIVATE_SESSION', 'WD02_PRIVATE_CREDENTIAL')

SEED = r'''import json,os,sys
from pathlib import Path
from workbench.tasks.repository import TaskRepository
from workbench.storage.work_records.store import RecordStore
from workbench.storage.log_raw.store import RawLogStore
os.umask(0o077);data=Path(sys.argv[1])
repo=TaskRepository(data/'tasks.sqlite3');task='dddddddd-dddd-4ddd-8ddd-dddddddddddd'
repo.create_task({'title':'WD02_PRIVATE_TASK one','goal':'preserve','command':'touch MUST_NOT_REPLAY'},task_id=task)
repo.revise_task(task,{'title':'WD02_PRIVATE_TASK two','goal':'preserve','command':'touch MUST_NOT_REPLAY'})
repo.approve_scope(task,2,{'command':'touch MUST_NOT_REPLAY'});repo.proceed(task,2,'historical fixture only')
run=repo.start_run(task,2,inputs={'worktree_path':str(data.parent)})
repo.record_shell_event(run,'ended',{'exit_status':0});repo.complete_run(run,{'historical':True})
records=RecordStore(data/'records.sqlite3',task_repository=repo)
first=records.create(title='private',body='WD02_PRIVATE_RECORD one',task_id=task,actor='manager')
assert first['status']=='saved'
record=first['record']['record_id']
assert records.revise(record,expected_revision=1,title='private',body='WD02_PRIVATE_RECORD two',task_id=task,actor='worker')['status']=='saved'
repo.close()
with RawLogStore(data/'raw-logs') as raw:raw.append(run,b'WD02_PRIVATE_RAW\x00\xff')
sessions=data/'omp-root/agent/sessions/doctor-fixture';sessions.mkdir(parents=True,mode=0o700)
(sessions/'history.jsonl').write_text(json.dumps({'type':'session','version':3,'id':'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee','timestamp':'2026-10-20T00:00:00Z','cwd':str(data.parent)})+'\n'+json.dumps({'type':'message','id':'fixture-message','parentId':None,'timestamp':'2026-10-20T00:00:00Z','message':{'role':'user','content':[{'type':'text','text':'WD02_PRIVATE_SESSION'}],'timestamp':0}})+'\n')
print(json.dumps({'task':task,'run':run,'record':record,'revisions':2}))
'''


class ZeroProvider(Provider):
    def parse_request(self):
        valid = super().parse_request()
        if valid:
            self.server.all_requests += 1
        return valid


class DoctorRun(UpdateRun):
    def __init__(self, *args):
        super().__init__(*args)
        self.provider.all_requests = 0
        self.provider.RequestHandlerClass = ZeroProvider
        self.report['inputs']['doctor_harness_sha256'] = digest(__file__)
        save(self.output/'input-hashes.json', self.report['inputs'])
        self.report['cases'] = []

    def inventory(self):
        result, payload = {}, {}
        selected = {'project': self.projects[0] if self.projects else self.private/'absent-project',
                    'auth': Path(self.env['HOME'])/'.omp/agent',
                    'state': Path(self.env['XDG_STATE_HOME']),
                    'fence': Path('/tmp/omp-workbench-maintenance-0')}
        for label, root in selected.items():
            if not root.exists():
                continue
            for path in (root, *sorted(root.rglob('*'))):
                key = label+'/'+str(path.relative_to(root))
                info = path.lstat()
                item = {'mode':stat.S_IMODE(info.st_mode), 'uid':info.st_uid, 'kind':'directory'}
                if path.is_symlink():
                    item.update(kind='symlink', target=os.readlink(path))
                elif path.is_file():
                    value = path.read_bytes()
                    item.update(kind='file', bytes=len(value), sha256=hashlib.sha256(value).hexdigest())
                    payload[key] = value
                result[key] = item
        return result, payload

    def doctor(self, name, expected_exit, expected, *, project=True, environment=None, live=False, text=False):
        before, original = self.inventory()
        command = self.command.with_name('omp-workbench') if text else self.command
        argv = [command, 'doctor']
        if project:
            argv += ['--data-dir', self.projects[0]/'.wb']
        if not text:
            argv += ['--json']
        code, out, err = self.run(argv, name, cwd=self.private, env=environment, timeout=35, check=False)
        after, current = self.inventory()
        delta = [key for key in before.keys() | after.keys() if before.get(key) != after.get(key)]
        record = {'name':name, 'exit':code, 'inventory_delta':delta, 'live':live}
        self.report['cases'].append(record)
        save(self.output/(name+'-before.json'), before)
        save(self.output/(name+'-after.json'), after)
        assert not any(marker in out+err for marker in PRIVATE), 'private sentinel leaked'
        assert not err, name+' unexpected stderr'
        assert code == expected_exit, name+' unexpected exit '+str(code)
        if text:
            for identifier, expected_status in expected.items():
                assert '['+expected_status+'] '+identifier+':' in out, name+' text status '+identifier
        else:
            report = json.loads(out)
            assert report['schema_version'] == 1
            checks = {item['id']:item for item in report['checks']}
            for identifier, expected_status in expected.items():
                assert checks[identifier]['status'] == expected_status, name+' status '+identifier+': '+checks[identifier]['status']
            assert ('error' if code else report['overall']) == report['overall']
            if checks['installation']['status'] == 'ok':
                assert self.report['refs']['previous'] in checks['installation']['summary']
        if not live:
            assert not delta, name+' modified inactive inventory: '+repr(delta)
        else:
            classified = []
            for key in delta:
                assert key in original and key in current, name+' created/deleted source '+key
                first, second = original[key], current[key]
                offsets = [index for index,(a,b) in enumerate(zip(first,second)) if a != b]
                if key == 'project/.wb/backend.log':
                    assert second.startswith(first), 'backend log rewritten'
                    classified.append({'path':key,'writer':'owned backend log append'})
                elif key.endswith('.sqlite3-shm') or key.endswith('/history.db-shm'):
                    assert len(first) == len(second) and set(offsets) <= set(range(100,120)), key+' non-reader SHM delta'
                    classified.append({'path':key,'writer':'SQLite reader marks','offsets':offsets})
                elif key == 'project/.wb/tasks.sqlite3':
                    assert len(first) == len(second) and set(offsets) <= set(range(24,28)) | set(range(92,96)), 'task payload changed'
                    classified.append({'path':key,'writer':'backend SQLite header counters','offsets':offsets})
                else:
                    raise AssertionError(name+' unexplained live source delta '+key)
            record['normal_writer_deltas'] = classified
            self.alive()
        assert self.provider.requests == self.provider.all_requests == 0, 'unexpected model/API request'
        assert not any((item/'MUST_NOT_REPLAY').exists() for item in self.projects)

    def alive(self):
        for reference in self.live_refs:
            assert state(reference['pid'],reference['start_ticks']) == 'alive', 'doctor ended owned process'

    def scenario(self):
        self.install()
        (Path(self.env['HOME'])/'.omp/agent/agent.db').chmod(0o600)
        self.doctor('no-project', 0, {'installation':'ok','project':'warning','omp':'ok'}, project=False)
        project, _ = self.project('doctor-project')
        for name, value in [('source.py',b'WD02_SOURCE\n'),('result.bin',b'WD02_RESULT\x00\xff')]:
            (project/name).write_bytes(value);(project/name).chmod(0o600)
        seed = self.private/'seed.py';seed.write_text(SEED)
        self.run([self.python,'-I',seed,project/'.wb'],'seed-private-history')
        healthy = {'installation':'ok','python':'ok','uv':'ok','omp':'ok','auth':'ok','project':'ok',
                   'permissions':'ok','db.tasks':'ok','db.records':'ok','backend':'warning','bridge':'warning',
                   'update':'ok','backup':'ok','resume':'ok'}
        self.doctor('healthy-inactive', 0, healthy)
        self.doctor('healthy-text-alias', 0, healthy, text=True)
        missing = dict(self.env, WORKBENCH_OMP=str(self.private/'missing-omp'))
        self.doctor('missing-omp', 1, {'omp':'error'}, environment=missing)
        records = project/'.wb/records.sqlite3';saved = records.read_bytes()
        records.write_bytes(b'WD02_PRIVATE_RECORD_CORRUPT')
        self.doctor('corrupt-db', 1, {'db.records':'error'})
        records.write_bytes(saved)
        updates = Path(self.env['XDG_STATE_HOME'])/'omp-workbench/updates'
        updates.mkdir(mode=0o700,parents=True);run=updates/'fixture-failed';run.mkdir(mode=0o700)
        manifest=run/'manifest.json'
        save(manifest,{'schema_version':1,'previous':self.report['refs']['previous'],
             'target':self.report['refs']['target'],'complete':False,'backup_complete':False,
             'installation':'unknown','projects':[],'source':'https://user:WD02_PRIVATE_CREDENTIAL@invalid/repo'})
        manifest.chmod(0o600)
        resume=project/'.wb/update-resume.json';save(resume,{'schema_version':1,'backup':str(run)});resume.chmod(0o600)
        fence=Path('/tmp/omp-workbench-maintenance-0');fence.mkdir(mode=0o700)
        descriptor=os.open(fence/'update.lock',os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.doctor('active-update-failed-backup-resume',1,{'update':'warning','backup':'error','resume':'warning'})
        finally:
            os.close(descriptor)
        shutil.rmtree(updates);resume.unlink()
        self.start(project,'start-owned-backend')
        snapshot=self.status(project,'ready-owned-backend')
        self.live_refs=[pane['process'] for pane in snapshot['panes'].values() if pane.get('process')]
        self.live_refs.append(snapshot['backend']['process'])
        self.report['live_processes']=self.live_refs
        live_expected=dict(healthy,backend='ok',bridge='ok')
        self.doctor('healthy-live',0,live_expected,live=True)
        backend=self.live_refs[-1]
        descriptor=os.pidfd_open(backend['pid'])
        try:
            self.alive();signal.pidfd_send_signal(descriptor,signal.SIGSTOP)
            self.doctor('unreachable-live',1,{'backend':'unknown','bridge':'unknown'},live=True)
            path=project/'.wb/backend.json';saved=path.read_bytes();record=json.loads(saved)
            record['processes']['backend']['start_ticks']+=1
            save(path,record);path.chmod(0o600)
            try:self.doctor('mismatched-live-identity',1,{'backend':'error','bridge':'unknown'},live=True)
            finally:path.write_bytes(saved)
        finally:
            signal.pidfd_send_signal(descriptor,signal.SIGCONT);os.close(descriptor)
        self.alive()
        self.installed('unchanged-install',self.report['refs']['previous'],'previous')
        self.report['checks'].update(actual_installed_doctor_boundaries=True,zero_model_or_api_requests=True)

    def cleanup(self):
        # The shared owner cleanup stops/reaps only this private namespace's
        # exact processes. Do not copy raw auth/DB/session bodies into reports.
        InstalledRun.cleanup(self)
        assert not self.report['remaining_processes'] and not self.report['unrecorded_private_processes']
        self.report['all_api_requests']=self.provider.all_requests
        self.report['private_processes_stopped']=True
        shutil.rmtree(self.private)
        self.report['private_root_removed']=True
        save(self.output/'report.json',self.report)


def execute(output,omp,uv,cache,host_namespace):
    run=DoctorRun(Path(output).absolute(),Path(omp),Path(uv),Path(cache),host_namespace)
    try:run.scenario()
    except BaseException as error:
        run.report['error']=f'{type(error).__name__}: {error}'
        raise
    finally:run.cleanup()


@unittest.skipUnless(os.environ.get('WB_LIVE_DOCTOR')=='1','opt-in actual isolated installed doctor')
class InstalledDoctor(unittest.TestCase):
    def test_actual_read_only_diagnostics(self):
        execute(os.environ['WB_DOCTOR_RUN_ROOT'],os.environ['WB_LIVE_OMP'],os.environ['WB_LIVE_UV'],
                os.environ['WB_LIVE_CACHE'],os.environ['WB_HOST_PID_NAMESPACE'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('run-root','omp','uv','cache','host-pid-namespace'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    execute(args.run_root,args.omp,args.uv,args.cache,args.host_pid_namespace)
