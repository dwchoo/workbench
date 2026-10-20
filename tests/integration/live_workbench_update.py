"""Opt-in installed update lifecycle inside a verified private Linux PID namespace.

Real uv/Git/OMP, loopback-only scripted replies, owned projects and no host WB.
Run only with the documented unshare + private /tmp mount wrapper. Namespace
PID 1 termination additionally bounds orphan lifetime; the harness first stops
and verifies its owned processes and retains only concise durable proof.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tests/backend/installation'))
from live_project_sessions import InstalledRun, state  # noqa: E402

OMP_SHA256 = 'bf1e86a935b1ea26166a5280001eccea1205e6c21efe1b5853f0fd368c754974'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def namespace_guard(host_namespace):
    current = os.readlink('/proc/self/ns/pid')
    mapping = Path('/proc/self/uid_map').read_text().split()
    assert os.getpid() == 1 and os.geteuid() == 0, 'run as PID 1 in the approved mapped-root namespace'
    assert host_namespace and current != host_namespace, 'host PID namespace is forbidden'
    assert int(os.readlink('/proc/self')) == os.getpid(), 'mount-proc must match the new PID namespace'
    assert len(mapping) == 3 and mapping[0] == '0' and mapping[2] == '1', 'one mapped host UID only'
    tmp_mounts = [line for line in Path('/proc/self/mountinfo').read_text().splitlines()
                  if line.split()[4] == '/tmp']
    assert tmp_mounts and ' - tmpfs ' in tmp_mounts[-1], 'private tmpfs /tmp is mandatory'
    assert not (Path('/tmp')/'omp-workbench-maintenance-0').exists(), 'unexpected preexisting fence'
    assert not (Path('/tmp')/'omp-workbench-0').exists(), 'unexpected preexisting WB runtime'
    return {'pid_namespace':current, 'host_pid_namespace':host_namespace, 'uid_map':mapping,
            'pid':os.getpid(), 'euid':os.geteuid(), 'tmp_mount':tmp_mounts[-1],
            'tmp_available_bytes':os.statvfs('/tmp').f_bavail*os.statvfs('/tmp').f_frsize}


SEED = r'''import json,sys
from pathlib import Path
from workbench.tasks.repository import TaskRepository
from workbench.storage.work_records.store import RecordStore
from workbench.storage.log_raw.store import RawLogStore
data=Path(sys.argv[1]); label=sys.argv[2]; project=data.parent
id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' if label=='A' else 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
repo=TaskRepository(data/'tasks.sqlite3')
spec={'title':'WU03 '+label,'goal':'preserve literal '+label,'command':'touch '+str(project/'MUST_NOT_REPLAY')}
repo.create_task(spec,task_id=id);repo.revise_task(id,dict(spec,title='WU03 revision two '+label))
repo.approve_scope(id,2,{'command':spec['command']});repo.proceed(id,2,'explicit historical fixture proceed')
run=repo.start_run(id,2,inputs={'worktree_path':str(project)})
repo.record_shell_event(run,'ended',{'exit_status':0,'history_only':True})
repo.complete_run(run,{'fixture':'already completed'})
store=RecordStore(data/'records.sqlite3',task_repository=repo)
first=store.create(title='원문 '+label,body='WU03 ORIGINAL '+label+'\n이전 원문',task_id=id,actor='manager')
assert first['status']=='saved',first
record=first['record']['record_id']
second=store.revise(record,expected_revision=1,title='변경 '+label,body='WU03 UPDATED '+label+'\n새 원문',task_id=id,actor='worker')
assert second['status']=='saved',second
expected={'task':id,'run':run,'record':record,'spec1':repo.get_task_spec(id,1),
 'spec2':repo.get_task_spec(id,2),'decisions':repo.get_decisions(id,2),
 'run_record':repo.get_run(run),'record1':store.get(record,1),'record2':store.get(record,2)}
repo.close()
with RawLogStore(data/'raw-logs') as raw:raw.append(run,('WU03_RAW_'+label).encode()+b'\x00\xff')
print(json.dumps(expected))
'''

WORKER_TURN = r'''import json,sys,time
from pathlib import Path
from workbench.backend.client import UiClient
from workbench.contracts.v1 import PaneId
from workbench.contracts.ui_v1 import ClientType
p=Path(sys.argv[1]);record=json.loads((p/'.wb/backend.json').read_text());marker=sys.argv[2]
with UiClient(Path(record['ui_socket']),expected_process=record['processes']['backend']) as c:
 assert c.attach()['ok']
 assert c.input(PaneId.WORKER_OMP,marker.encode())['ok']
 assert c.input(PaneId.WORKER_OMP,b'\r')['ok']
 deadline=time.monotonic()+40
 while time.monotonic()<deadline:
  files=list((p/'.wb/omp-root/agent/sessions').rglob('*.jsonl'))
  pending=c.request(ClientType.SHUTDOWN_REQUEST)
  if any(marker in f.read_text() and 'WI05_LOOPBACK_REPLY' in f.read_text() for f in files) and not pending['active']:break
  c.pump(.1)
 else:raise AssertionError('worker reply or idle evidence missing')
 c.detach()
 print(json.dumps({'pending':pending,'snapshot':c.snapshot(),'session_files':[str(f) for f in files]}))
'''

HOST_COMMAND = r'''import json,sys,time
from pathlib import Path
from workbench.backend.client import UiClient
from workbench.contracts.v1 import PaneId
p=Path(sys.argv[1]);r=json.loads((p/'.wb/backend.json').read_text())
destination=Path(sys.argv[3]) if len(sys.argv)>3 else p/'running.pid'
with UiClient(Path(r['ui_socket']),expected_process=r['processes']['backend']) as c:
 assert c.attach()['ok']
 assert c.input(PaneId.HOST_SHELL,(sys.argv[2]+'\r').encode())['ok']
 deadline=time.monotonic()+15
 while time.monotonic()<deadline and (not destination.exists() or not destination.stat().st_size):c.pump(.05)
 assert destination.exists() and destination.stat().st_size,'manual command did not reach expected boundary'
 c.detach()
'''

SCOPE = r'''import argparse,json,sys
from pathlib import Path
from workbench.backend.maintenance import discover
expected=Path(sys.argv[1]); result=[]
for i in discover(argparse.Namespace(data_dir=None)):
 assert i.layout.root.is_relative_to(expected),'external data root: '+str(i.layout.root)
 assert i.project_root and i.project_root.is_relative_to(expected),'external project'
 result.append({'project':str(i.project_root),'data':str(i.layout.root),'state':i.state})
print(json.dumps(result))
'''


class UpdateRun(InstalledRun):
    def __init__(self, output, omp, uv, cache, host_namespace):
        isolation = namespace_guard(host_namespace)
        assert digest(omp) == OMP_SHA256, 'approved existing OMP binary hash changed'
        super().__init__(output, omp)
        self.uv = Path(uv).resolve(strict=True)
        self.cache_seed = Path(cache).resolve(strict=True)
        self.report['isolation'] = isolation
        self.report['omp_sha256'] = digest(omp)
        inputs={}
        for directory in ('src','omp_bridge','build_support'):
            for path in (ROOT/directory).rglob('*'):
                if path.is_file() and not any(part in ('__pycache__','_assets') or part.endswith('.egg-info')
                    for part in path.relative_to(ROOT).parts) and path.suffix!='.pyc':
                    inputs[str(path.relative_to(ROOT))]=digest(path)
        for name in ('pyproject.toml','MANIFEST.in'):
            inputs[name]=digest(ROOT/name)
        self.report['inputs']={'packaging':inputs,'harness_sha256':digest(__file__),
                              'installed_harness_sha256':digest(ROOT/'tests/backend/installation/live_project_sessions.py')}
        save(self.output/'input-hashes.json',self.report['inputs'])
        self.env.update(PATH=str(self.uv.parent)+':/usr/bin:/bin', UV_OFFLINE='1', UV_LINK_MODE='copy',
            UV_NO_PROGRESS='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        self.expected = {}
        self.protected = {}

    def install(self):
        shutil.copytree(self.cache_seed,self.env['UV_CACHE_DIR'],dirs_exist_ok=True,symlinks=False)
        source=self.private/'source';source.mkdir()
        for name in ('src','omp_bridge','build_support'):
            shutil.copytree(ROOT/name,source/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info','_assets'))
        for name in ('pyproject.toml','MANIFEST.in'):shutil.copy2(ROOT/name,source/name)
        self.run(['git','init','-q','-b','main',source],'git-init')
        refs={}
        for label in ('previous','target'):
            (source/'src/workbench/_update_integration_marker.py').write_text('MARKER='+repr(label)+'\n')
            self.run(['git','-C',source,'add','.'],'git-add-'+label)
            self.run(['git','-C',source,'-c','user.name=WU03 fixture','-c','user.email=wu03@invalid',
                '-c','commit.gpgsign=false','commit','-qm',label],'git-commit-'+label)
            _,out,_=self.run(['git','-C',source,'rev-parse','HEAD'],'git-ref-'+label)
            refs[label]=out.strip()
        self.report['refs']=refs; self.source=source
        self.run([self.uv,'tool','install','--offline','--python',sys.executable,
                  'git+'+source.as_uri()+'@'+refs['previous']],'install-previous')
        self.env['WORKBENCH_OMP']=str(self.omp)
        for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            self.env[key]='http://127.0.0.1:9'
        self.env.update(NO_PROXY='127.0.0.1',no_proxy='127.0.0.1')
        auth=Path(self.env['HOME'])/'.omp/agent';auth.mkdir(mode=0o700,parents=True)
        (auth/'agent.db').write_bytes(b'')
        self.installed('previous-install',refs['previous'],'previous')

    def installed(self,name,commit,marker=None):
        code='''import importlib.metadata as m,json,sys
from workbench import _update_integration_marker as marker
s=json.loads(m.distribution('workbench').read_text('direct_url.json'))
print(json.dumps({'source':s,'marker':marker.MARKER,'python':sys.executable,'installer':m.distribution('workbench').read_text('INSTALLER')}))'''
        _,out,_=self.run([self.python,'-I','-c',code],name)
        identity=json.loads(out)
        assert identity['source']['vcs_info']['commit_id']==commit,identity
        assert identity['installer'].strip()=='uv'
        if marker:assert identity['marker']==marker
        save(self.output/(name+'.json'),identity)
        return identity

    def scope(self,name):
        _,out,_=self.run([self.python,'-I','-c',SCOPE,self.private],name)
        result=json.loads(out)
        assert {item['project'] for item in result}==set(map(str,self.projects)),result
        return result

    def protected_bytes(self):
        result={}
        for project in self.projects:
            for path in (project/'source.py',project/'result.bin',project/'execution-count'):
                if path.exists():result[str(path)]=digest(path)
            data=project/'.wb'
            for path in data.rglob('*'):
                if path.is_symlink() or not path.is_file():continue
                relative=str(path.relative_to(data))
                if (relative.startswith('omp-root/agent/sessions/') or relative in ('automation.json','records.sqlite3',
                    'tasks.sqlite3') or relative.startswith('records.sqlite3-')):
                    result[str(path)]=digest(path)
        return result

    def update_tty(self,name,answer,before_answer=None):
        # Diagnostic: no harness-owned source SQLite readers may retain a
        # writable WAL/SHM descriptor when the updater checks writer absence.
        held=[]
        for descriptor in Path('/proc/self/fd').iterdir():
            try:
                target=os.readlink(descriptor)
                if any(target.startswith(str(project/'.wb')+'/') for project in self.projects):
                    held.append({'fd':descriptor.name,'target':target,
                        'fdinfo':(Path('/proc/self/fdinfo')/descriptor.name).read_text()})
            except FileNotFoundError:
                continue
        self.report[name+'-harness-source-fds']=held
        self.scope(name+'-scope-before')
        master,slave=os.openpty()
        child=subprocess.Popen([self.command,'update','--ref','main'],cwd=self.private,env=self.env,
            stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
        os.close(slave);body=bytearray();sent=False
        try:
            deadline=time.monotonic()+210
            while True:
                if select.select([master],[],[],.05)[0]:
                    try:chunk=os.read(master,65536)
                    except OSError:chunk=b''
                    if chunk:body.extend(chunk)
                if b'[y/N]' in body and not sent:
                    rows=[line for line in bytes(body).replace(b'\r',b'').splitlines() if line.startswith(b'[{')]
                    assert rows,'scope display missing'
                    shown=json.loads(rows[-1])
                    assert shown and all(Path(item['project']).is_relative_to(self.private)
                        and item['project'] in set(map(str,self.projects)) for item in shown),shown
                    self.scope(name+'-scope-at-prompt')
                    if before_answer is not None:before_answer()
                    os.write(master,answer.encode()+b'\n');sent=True
                status=os.waitid(os.P_PID,child.pid,os.WEXITED|os.WNOHANG|os.WNOWAIT)
                if status is not None:
                    # Drain any final PTY bytes before reaping the reserved leader.
                    for _ in range(10):
                        if not select.select([master],[],[],.02)[0]:break
                        try:chunk=os.read(master,65536)
                        except OSError:break
                        if not chunk:break
                        body.extend(chunk)
                    result=child.wait(timeout=5);break
                if time.monotonic()>deadline:raise TimeoutError(name)
            assert sent,'expected real active-work confirmation'
            self.report[name]={'exit':result,'scope_checked_at_prompt':True}
            return result,bytes(body).decode(errors='replace')
        finally:
            if child.returncode is None:
                os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=10)
            os.close(master)
            (self.output/(name+'.pty')).write_bytes(body)

    def prepare_projects(self):
        for label in ('A','B'):
            project,context=self.project('project-'+label)
            (project/'source.py').write_text('WU03_SOURCE_'+label+'\n')
            (project/'result.bin').write_bytes(('WU03_RESULT_'+label).encode()+b'\x00\xff')
            _,out,_=self.run([self.python,'-I','-c',SEED,project/'.wb',label],'seed-'+label)
            self.expected[str(project)]=json.loads(out)
            self.start(project,'start-'+label)
            self.exercise(project,context,label)
            self.run([self.python,'-I','-c',WORKER_TURN,project,'WU03_WORKER_'+label],'worker-turn-'+label,timeout=55)
            snapshot=self.status(project,'ready-'+label)
            self.report['sessions_'+label]=snapshot['bridge']
        save(self.output/'expected-durable.json',self.expected)
        self.scope('all-projects-owned')
        self.managed_caller_boundary()
        self.background_boundary()
        project=self.projects[0]
        script=project/'active-command.sh'
        script.write_text('#!/bin/sh\nprintf "START\\n" >> '+shlex.quote(str(project/'execution-count'))+
                          '\nprintf "%s\\n" "$$" > '+shlex.quote(str(project/'running.pid'))+'\nexec sleep 120\n')
        self.run([self.python,'-I','-c',HOST_COMMAND,project,'/bin/sh '+shlex.quote(str(script))],
                 'active-private-host-command',timeout=25)
        pid=int((project/'running.pid').read_text())
        ticks=int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        assert state(pid,ticks)=='alive'
        self.report['active_command']={'pid':pid,'start_ticks':ticks,'count':(project/'execution-count').read_text()}

    def input_line(self,line,destination,name):
        self.run([self.python,'-I','-c',HOST_COMMAND,self.projects[0],line,destination],name,timeout=25)

    def wait_prompt(self,name):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            snapshot=self.status(self.projects[0],name)
            if snapshot['panes']['host_shell']['shell']['parent_mode']=='manual_prompt':return
            time.sleep(.05)
        raise AssertionError('owned shell did not return to prompt')

    def end_exact(self,pid,ticks):
        assert state(pid,ticks)=='alive'
        descriptor=os.pidfd_open(pid)
        try:signal.pidfd_send_signal(descriptor,signal.SIGTERM)
        finally:os.close(descriptor)
        deadline=time.monotonic()+8
        while state(pid,ticks)=='alive' and time.monotonic()<deadline:time.sleep(.02)
        assert state(pid,ticks)=='ended'

    def managed_caller_boundary(self):
        project=self.projects[0];out=project/'managed-caller.out';done=project/'managed-caller.exit'
        environment=' '.join(shlex.quote(key+'='+value) for key,value in self.env.items())
        line='env '+environment+' '+shlex.quote(str(self.command))+' update --ref main > '+shlex.quote(str(out))+' 2>&1; printf "%s" "$?" > '+shlex.quote(str(done))
        self.input_line(line,done,'managed-caller')
        assert done.read_text()=='1' and '외부 terminal' in out.read_text(),out.read_text()
        assert not (Path(self.env['XDG_STATE_HOME'])/'omp-workbench/updates').exists()
        self.scope('managed-caller-still-running')
        (self.output/'managed-caller.out').write_bytes(out.read_bytes())
        self.wait_prompt('after-managed-caller')
        self.report['checks']['actual_managed_caller_refused_before_any_shutdown']=True

    def background_boundary(self):
        path=self.projects[0]/'background.pid'
        self.input_line('sleep 120 & echo $! > '+shlex.quote(str(path)),path,'start-background')
        self.wait_prompt('background-prompt')
        pid=int(path.read_text());ticks=int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        result,text=self.update_tty('background-declined','n')
        assert result==1 and 'shell_jobs' in text and str(pid) in text,text
        assert state(pid,ticks)=='alive'
        assert not (Path(self.env['XDG_STATE_HOME'])/'omp-workbench/updates').exists()
        self.report['background_command']={'pid':pid,'start_ticks':ticks}
        self.report['checks']['actual_background_decline_preserves_job']=True

    def foreground_replacement_boundary(self):
        original=self.report['active_command'];replacement=self.projects[0]/'replacement.pid'
        def replace():
            self.end_exact(original['pid'],original['start_ticks'])
            self.wait_prompt('race-return-prompt')
            self.input_line("/bin/sh -c 'echo $$ > "+str(replacement)+"; exec sleep 120'",replacement,'race-new-foreground')
        result,text=self.update_tty('foreground-replaced','y',replace)
        assert result==1 and '업데이트 완료' not in text,text
        pid=int(replacement.read_text());ticks=int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        assert state(pid,ticks)=='alive'
        manifests=list((Path(self.env['XDG_STATE_HOME'])/'omp-workbench/updates').glob('*/manifest.json'))
        assert len(manifests)==1
        manifest=json.loads(manifests[0].read_text())
        assert not manifest['backup_complete'] and manifest['installation']=='not_started'
        assert not any(item['shutdown'] for item in manifest['projects'])
        save(self.output/'foreground-replaced-manifest.json',manifest)
        self.scope('race-refusal-still-running')
        self.end_exact(pid,ticks);self.wait_prompt('race-clean-prompt')
        next_path=self.projects[0]/'final-foreground.pid'
        self.input_line("/bin/sh -c 'echo $$ > "+str(next_path)+"; exec sleep 120'",next_path,'final-foreground')
        pid=int(next_path.read_text());ticks=int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        self.report['active_command']={'pid':pid,'start_ticks':ticks,'count':'START\n'}
        self.report['checks']['actual_foreground_replacement_refused_before_stop']=True

    def verify_backup(self,manifest_path):
        manifest=json.loads(manifest_path.read_text())
        assert manifest['backup_complete'] and len(manifest['projects'])==2,manifest
        for item in manifest['projects']:
            project=Path(item['project_root']);assert project in self.projects
            root=manifest_path.parent/item['backup']
            assert root.stat().st_mode&0o777==0o700
            for entry in item['files']:
                copied=root/entry['path']
                assert digest(copied)==entry['sha256']
                assert copied.stat().st_mode&0o777==0o600
                assert digest(Path(item['data_root'])/entry['path'])==entry['sha256'],entry['path']
                assert not any(part.startswith('agent.db') for part in Path(entry['path']).parts)
            expected=self.expected[str(project)]
            for name in ('tasks.sqlite3','records.sqlite3'):
                # Stopped SQLite copies have no live writer. Read only this backup.
                with closing(sqlite3.connect('file:'+str(root/name)+'?mode=ro',uri=True)) as db:
                    if name=='records.sqlite3':
                        rows=db.execute('SELECT revision,body,actor FROM revisions WHERE record_id=? ORDER BY revision',
                                        (expected['record'],)).fetchall()
                        label=project.name[-1]
                        assert rows==[(1,'WU03 ORIGINAL '+label+'\n이전 원문','manager'),
                                      (2,'WU03 UPDATED '+label+'\n새 원문','worker')],rows
                    else:
                        rows=db.execute('SELECT revision,spec_json FROM task_specs WHERE task_id=? ORDER BY revision',
                                        (expected['task'],)).fetchall()
                        assert [row[0] for row in rows]==[1,2]
                        assert json.loads(rows[1][1])==expected['spec2']['spec']
            assert not (root/'omp-root/agent/agent.db').exists()
            assert not (root/'omp-root/cache').exists()
            assert list((root/'omp-root/agent/sessions').rglob('*.jsonl'))
        save(self.output/'verified-backup-manifest.json',manifest)
        return manifest

    def scenario(self):
        self.install();self.prepare_projects()
        before=self.protected_bytes(); requests=self.provider.requests
        save(self.output/'refusal-before-hashes.json',before)
        for path in before:
            proof=self.output/'refusal-before-bytes'/Path(path).relative_to(self.private)
            proof.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,proof)
        result,text=self.update_tty('declined','n')
        assert result==1 and '업데이트 취소' in text,text
        after=self.protected_bytes()
        save(self.output/'refusal-after-hashes.json',after)
        for path in after:
            proof=self.output/'refusal-after-bytes'/Path(path).relative_to(self.private)
            proof.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,proof)
        delta=[path for path in before.keys()|after.keys() if before.get(path)!=after.get(path)]
        self.report['refusal_changed_paths']=delta
        save(self.output/'refusal-delta.json',delta)
        # A live TaskRepository connection may increment only SQLite's change
        # counter/version-valid-for header. All payload/schema bytes stay exact.
        self.report['refusal_sqlite_header_only']=[]
        for path in delta:
            original=(self.output/'refusal-before-bytes'/Path(path).relative_to(self.private)).read_bytes()
            current=Path(path).read_bytes()
            offsets=[index for index,(a,b) in enumerate(zip(original,current)) if a!=b]
            assert (Path(path).name=='tasks.sqlite3' and len(original)==len(current)
                    and set(offsets)<=set(range(24,28))|set(range(92,96))), (path,offsets)
            self.report['refusal_sqlite_header_only'].append({'path':path,'offsets':offsets})
        ref=self.report['active_command'];assert state(ref['pid'],ref['start_ticks'])=='alive'
        assert self.provider.requests==requests
        assert not (Path(self.env['XDG_STATE_HOME'])/'omp-workbench/updates').exists()
        self.installed('after-refusal',self.report['refs']['previous'],'previous')
        self.foreground_replacement_boundary()
        result,text=self.update_tty('approved','y')
        assert result==0 and '업데이트 완료' in text,text
        ref=self.report['active_command']
        assert state(ref['pid'],ref['start_ticks'])=='ended'
        background=self.report['background_command']
        assert state(background['pid'],background['start_ticks'])=='ended'
        self.report['checks']['actual_background_approved_shutdown_ends_job']=True
        manifests=list((Path(self.env['XDG_STATE_HOME'])/'omp-workbench/updates').glob('*/manifest.json'))
        manifests=[path for path in manifests if json.loads(path.read_text())['complete']]
        assert len(manifests)==1
        manifest=self.verify_backup(manifests[0])
        assert manifest['installation']=='verified' and manifest['target']==self.report['refs']['target']
        self.installed('after-update',self.report['refs']['target'],'target')
        requests=self.provider.requests
        stopped={str(p):digest(p) for project in self.projects for p in
                 (project/'.wb/omp-root/agent/sessions').rglob('*.jsonl')}
        for index,project in enumerate(self.projects):
            _,out,_=self.start(project,'restart-'+str(index))
            assert '업데이트 재개 안내' in out and str(manifests[0].parent) in out,out
            snapshot=self.status(project,'restarted-status-'+str(index))
            assert snapshot['automation']['paused'],snapshot['automation']
            assert not (project/'MUST_NOT_REPLAY').exists()
            assert (project/'source.py').read_text()=='WU03_SOURCE_'+project.name[-1]+'\n'
            assert (project/'result.bin').read_bytes()==('WU03_RESULT_'+project.name[-1]).encode()+b'\x00\xff'
        assert all(digest(path)==expected for path,expected in stopped.items())
        assert (self.projects[0]/'execution-count').read_text()=='START\n'
        assert self.provider.requests==requests,'restart made an automatic model turn/replay'
        self.report['checks'].update(actual_tty_decline_no_mutation=True,actual_active_stop_backup_selfreplace=True,
            literal_all_record_revisions_Task_sessions_preserved=True,actual_restart_paused_no_replay=True)
        self.failure()

    def failure(self):
        self.scope('failure-owned-scope')
        shim=self.private/'failure-bin';shim.mkdir()
        empty=self.private/'empty-cache';empty.mkdir()
        script=shim/'uv'
        script.write_text('#!/bin/sh\ncase " $* " in *" --offline "*) export UV_CACHE_DIR='+shlex.quote(str(empty))+';; esac\nexec '+shlex.quote(str(self.uv))+' "$@"\n')
        script.chmod(0o700)
        environment=dict(self.env,PATH=str(shim)+':'+self.env['PATH'])
        code,out,err=self.run([self.command.with_name('omp-workbench'),'update','--ref',self.report['refs']['previous']],
            'actual-uv-offline-cache-failure',cwd=self.private,env=environment,check=False,timeout=210)
        assert code==1 and '수동 재설치' in err and '업데이트 완료' not in out,(code,out,err)
        manifests=[p for p in (Path(self.env['XDG_STATE_HOME'])/'omp-workbench/updates').glob('*/manifest.json')
                   if json.loads(p.read_text())['target']==self.report['refs']['previous']]
        assert len(manifests)==1
        manifest=json.loads(manifests[0].read_text())
        assert manifest['backup_complete'] and not manifest['complete'] and manifest['installation']=='unknown'
        assert manifest['uv_returncode']!=0
        assert manifest['previous']==self.report['refs']['target']
        assert str(manifests[0].parent) in err
        self.report['checks']['real_uv_failure_after_backup_no_false_success']=True
        self.report['failure']={'manifest':manifest,'injection':'real uv final offline call with empty private UV_CACHE_DIR; all commands exec actual uv'}
        save(self.output/'failure-manifest.json',manifest)
        self.run([self.command,'--help'],'entrypoint-after-failure',check=False)

    def cleanup(self):
        manifests=[]
        for path in (Path(self.env['XDG_STATE_HOME'])/'omp-workbench/updates').glob('*/manifest.json'):
            manifests.append({'path':str(path),'manifest':json.loads(path.read_text())})
        save(self.output/'maintenance-manifests-before-cleanup.json',manifests)
        for project in self.projects:
            for name in ('backend.json','backend.log'):
                path=project/'.wb'/name
                if path.is_file():
                    (self.output/(project.name+'-'+name)).write_bytes(path.read_bytes()[-65536:])
        super().cleanup()
        assert not self.report['remaining_processes'] and not self.report['unrecorded_private_processes'],self.report
        # Keep only minimal DB/session/raw/manifest evidence, no private home/cache.
        proof=self.output/'durable-proof';proof.mkdir(exist_ok=True,mode=0o700)
        for project in self.projects:
            destination=proof/project.name
            destination.mkdir(mode=0o700)
            for name in ('tasks.sqlite3','tasks.sqlite3-wal','tasks.sqlite3-shm','records.sqlite3','records.sqlite3-wal','records.sqlite3-shm','update-resume.json','automation.json'):
                path=project/'.wb'/name
                if path.is_file():shutil.copy2(path,destination/name)
            sessions=project/'.wb/omp-root/agent/sessions'
            if sessions.exists():shutil.copytree(sessions,destination/'sessions')
        self.report['private_processes_stopped']=True
        self.report['provider_requests']=self.provider.requests
        save(self.output/'report.json',self.report)
        shutil.rmtree(self.private)
        self.report['private_root_removed']=True
        save(self.output/'report.json',self.report)


def execute(output,omp,uv,cache,host_namespace):
    run=UpdateRun(Path(output).absolute(),Path(omp),Path(uv),Path(cache),host_namespace)
    try:
        run.scenario()
    except BaseException as error:
        run.report['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        run.cleanup()


@unittest.skipUnless(os.environ.get('WB_LIVE_UPDATE')=='1','opt-in real isolated installed update')
class InstalledUpdate(unittest.TestCase):
    def test_real_update_lifecycle(self):
        execute(os.environ['WB_UPDATE_RUN_ROOT'],os.environ['WB_LIVE_OMP'],os.environ['WB_LIVE_UV'],
                os.environ['WB_LIVE_CACHE'],os.environ['WB_HOST_PID_NAMESPACE'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root',required=True)
    parser.add_argument('--omp',required=True)
    parser.add_argument('--uv',required=True)
    parser.add_argument('--cache',required=True)
    parser.add_argument('--host-pid-namespace',required=True)
    args=parser.parse_args()
    execute(args.run_root,args.omp,args.uv,args.cache,args.host_pid_namespace)
