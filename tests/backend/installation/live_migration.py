"""G-WI-08: installed stopped legacy -> project copy -> reader reopen -> fresh start.

Uses explicit --omp / WB_LIVE_OMP, private HOME, fake auth and one loopback turn.
Raw/important/Task fixtures use public storage APIs; no experiment is executed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

from live_project_sessions import InstalledRun, state


def snapshot(root):
    result = {}
    for path in (root, *sorted(root.rglob('*'))):
        info = path.lstat()
        key = str(path.relative_to(root))
        row = {'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
               'mode': stat.S_IMODE(info.st_mode), 'inode': info.st_ino, 'device': info.st_dev}
        if stat.S_ISLNK(info.st_mode):
            row['link'] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            if not key.startswith('omp-root/agent/agent.db'):
                row['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                row['credential_metadata_only'] = True
        result[key] = row
    return result


TURN = r'''import json,sys,time
from pathlib import Path
from workbench.backend.client import UiClient
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId
data=Path(sys.argv[1]);record=json.loads((data/'backend.json').read_text())
with UiClient(Path(record['ui_socket']),expected_process=record['processes']['backend']) as client:
    assert client.attach()['ok']
    assert client.input(PaneId.MANAGER_OMP,b'WI06_MIGRATE_TURN')['ok']
    assert client.input(PaneId.MANAGER_OMP,b'\r')['ok']
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        files=list((data/'omp-root/agent/sessions').rglob('*.jsonl'))
        if any('WI05_LOOPBACK_REPLY' in path.read_text() for path in files):break
        client.pump(.1)
    else:raise AssertionError('actual loopback turn did not reach a durable session')
    assert client.request(ClientType.PAUSE)['ok']
    assert client.snapshot()['automation']['paused']
    client.detach()
print(json.dumps({'session_files':[str(path) for path in files],'paused':True}))
'''

SEED = r'''import json,sys
from pathlib import Path
from workbench.tasks.repository import TaskRepository
from workbench.storage.log_raw.store import RawLogStore
from workbench.storage.important_log import ImportantLogCatalog
data,worktree,sentinel=map(Path,sys.argv[1:])
task='88888888-8888-4888-8888-888888888888';command='99999999-9999-4999-8999-999999999999'
spec={'title':'WI06 fixed Task','command':'touch '+str(sentinel),'source_literal':str(data)}
repo=TaskRepository(data/'tasks.sqlite3')
repo.create_task(spec,task_id=task);repo.revise_task(task,dict(spec,title='WI06 revision two'))
repo.set_session_settings({'fixture':'WI06'})
repo.approve_scope(task,2,{'command':spec['command']});repo.proceed(task,2,'historical fixture proceed')
run=repo.start_run(task,2,inputs={'worktree_path':str(worktree)})
repo.record_shell_event(run,'ended',{'exit_status':0,'history_only':True})
repo.complete_run(run,{'fixture':'already completed'})
expected={'task':task,'run':run,'command':command,'spec1':repo.get_task_spec(task,1),
          'spec2':repo.get_task_spec(task,2),'run_record':repo.get_run(run),
          'decisions':repo.get_decisions(task,2),'run_events':repo.get_run_history(run),
          'shell_events':repo.get_shell_history(run)}
repo.close()
raw=b'WI06_RAW_FIXED\x00\xff '+str(data).encode()
with RawLogStore(data/'raw-logs') as store:store.append(run,raw)
directory=data/'workflow/runs'/run;directory.mkdir(mode=0o700)
record={'task_id':task,'revision':2,'run_id':run,'raw_log':str(data/'raw-logs'/f'{run}.log'),
        'result':str(directory/'run.json'),'command':spec['command'],'cwd':str(worktree),
        'shell_state':'ended','exit_status':0,'exit_confirmed':True}
(directory/'run.json').write_text(json.dumps(record));(directory/'run.json').chmod(0o600)
terminal=data/'workflow/terminal';terminal.mkdir(mode=0o700,exist_ok=True)
(terminal/f'{command}.log').write_bytes(b'WI06_COMMAND_FIXED');(terminal/f'{command}.log').chmod(0o600)
subject={'schemaVersion':1,'kind':'command','id':command}
body={'schemaVersion':1,'facts':['WI06_IMPORTANT_FIXED '+str(data)],'interpretations':[],'unknowns':[]}
catalog=ImportantLogCatalog(data/'important-logs');entry=catalog.append(subject,'event',[],body)
assert entry['state']=='ok',entry
expected.update(raw_hex=raw.hex(),important_subject=subject,important_body=body,
                original_command=spec['command'],worktree=str(worktree))
print(json.dumps(expected))
'''

REOPEN = r'''import base64,json,sqlite3,sys
from pathlib import Path
from workbench.tasks.repository import TaskRepository
from workbench.storage.log_raw.port import raw_query
from workbench.storage.log_raw.store import RawLogStore
from workbench.storage.important_log import ImportantLogReader
target=Path(sys.argv[1]);expected=json.loads(Path(sys.argv[2]).read_text())
repo=TaskRepository(target/'tasks.sqlite3')
assert repo.get_task_spec(expected['task'],1)==expected['spec1']
assert repo.get_task_spec(expected['task'],2)==expected['spec2']
assert repo.get_run(expected['run'])==expected['run_record']
assert repo.get_decisions(expected['task'],2)==expected['decisions']
assert repo.get_run_history(expected['run'])==expected['run_events']
assert repo.get_shell_history(expected['run'])==expected['shell_events']
assert repo.get_current_run(expected['task']) is None
repo.close()
raw=raw_query(target,'run',expected['run'])
assert base64.b64decode(raw['read']['dataBase64']).hex()==expected['raw_hex']
command=raw_query(target,'command',expected['command'])
assert base64.b64decode(command['read']['dataBase64'])==b'WI06_COMMAND_FIXED'
with RawLogStore(target/'raw-logs') as store:assert store.status(expected['run']).storage_error is None
important=ImportantLogReader(target/'important-logs').show(expected['important_subject'])
assert important['complete'],important
assert [entry['entry']['body'] for entry in important['entries']]==[expected['important_body']]
record=json.loads((target/'workflow/runs'/expected['run']/'run.json').read_text())
assert record['command']==expected['original_command'] and record['cwd']==expected['worktree']
assert record['raw_log']==str(target/'raw-logs'/f"{expected['run']}.log")
assert record['result']==str(target/'workflow/runs'/expected['run']/'run.json')
with sqlite3.connect(target/'omp-root/agent/history.db') as history:
    assert history.execute('SELECT prompt FROM history WHERE prompt=?',('WI06_MIGRATE_TURN',)).fetchall()==[('WI06_MIGRATE_TURN',)]
sessions=list((target/'omp-root/agent/sessions').rglob('*.jsonl'))
assert any('WI06_MIGRATE_TURN' in p.read_text() and 'WI05_LOOPBACK_REPLY' in p.read_text() for p in sessions)
print(json.dumps({'Task/revision/run/decision/shell IDs':True,'legacy_raw_command_bytes':True,
                  'important_public_reader':True,'OMP_session_history':True,'no_active_run':True}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root',type=Path,required=True)
    parser.add_argument('--omp',type=Path,help='verified fixture executable, or WB_LIVE_OMP; no discovery/download')
    args=parser.parse_args()
    chosen=args.omp or os.environ.get('WB_LIVE_OMP')
    if not chosen or not Path(chosen).is_file() or not os.access(chosen,os.X_OK):
        parser.error('supply existing executable --omp PATH or WB_LIVE_OMP; private loopback/fake auth only')
    run=InstalledRun(args.run_root.absolute(),chosen)
    source=None
    try:
        run.install()
        run.report['omp_binary']={'path':str(run.omp),'sha256':hashlib.sha256(run.omp.read_bytes()).hexdigest()}
        legacy=run.private/'legacy';legacy.mkdir(mode=0o700);run.projects.append(legacy)
        source=legacy/'.wb';agent=source/'omp-root/agent';agent.mkdir(parents=True,mode=0o700);source.chmod(0o700)
        (agent/'models.yml').write_bytes(run.model.read_bytes())
        argv=[run.command,'start','--data-dir',source,'--no-attach','--timeout','120']
        argv += [f'--omp-arg={value}' for value in ('--no-pty','--no-rules','--model','wi-project/scripted','--config',str(run.model))]
        run.run(argv,'legacy-start',cwd=legacy,timeout=150)
        run.run([run.python,'-I','-c',TURN,source],'legacy-one-loopback-turn',timeout=50)
        assert run.provider.requests==1,run.provider.requests
        run.run([run.command,'shutdown','--yes','--json','--data-dir',source],'legacy-confirmed-shutdown',cwd=legacy,timeout=65)
        old=json.loads((source/'backend.json').read_text())
        deadline=time.monotonic()+6
        while state(old['processes']['backend']['pid'],old['processes']['backend']['start_ticks'])!='ended' and time.monotonic()<deadline:time.sleep(.03)
        assert all(state(ref['pid'],ref['start_ticks'])=='ended' for ref in old['processes'].values()),old
        assert old['shutdown']['verified']
        repository=run.private/'fixture-repository'
        run.run(['/usr/bin/git','init',repository],'git-fixture-init')
        run.run(['/usr/bin/git','-c','user.name=WI06 fixture','-c','user.email=wi06@example.invalid','-c','commit.gpgsign=false',
                 '-C',repository,'commit','--allow-empty','-m','owned migration fixture'],'git-fixture-commit')
        worktree=source/'workflow/worktrees/original'
        run.run(['/usr/bin/git','-C',repository,'worktree','add','--detach',worktree,'HEAD'],'git-fixture-worktree')
        sentinel=run.private/'SHOULD_NEVER_EXECUTE'
        _,text,_=run.run([run.python,'-I','-c',SEED,source,worktree,sentinel],'seed-public-stores')
        expected=json.loads(text);expected_path=run.output/'expected-content.json';expected_path.write_text(json.dumps(expected,indent=2))
        before=snapshot(source);(run.output/'source-before.json').write_text(json.dumps(before,indent=2))
        sessions={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (source/'omp-root/agent/sessions').rglob('*.jsonl')}
        project=run.private/'new-research';project.mkdir(mode=0o700);run.projects.append(project)
        _,text,_=run.run([run.command,'migrate','--from',source,'--project',project],'actual-migrate',cwd=project,timeout=120)
        run.report['migration_result']=json.loads(text)
        assert snapshot(source)==before,'source changed during migration'
        target=project/'.wb'
        assert not (target/'backend.json').exists() and not (target/'backend-starting.json').exists()
        assert not (target/'workflow/worktrees/original').exists()
        manifest=json.loads((target/'migration-manifest.json').read_text())
        assert str(worktree) in json.dumps(manifest['source_dependencies'])
        assert not (target/'raw-rotation.json').exists() and not (target/'summary-compatibility').exists()
        assert all(hashlib.sha256((target/relative).read_bytes()).hexdigest()==digest for relative,digest in sessions.items())
        _,text,_=run.run([run.python,'-I','-c',REOPEN,target,expected_path],'public-reader-reopen',timeout=45)
        run.report['reader_reopen']=json.loads(text)
        assert snapshot(source)==before,'source changed during target readers'
        assert not sentinel.exists() and run.provider.requests==1
        run.start(project,'migrated-fresh-start',timeout=120)
        fresh=run.status(project,'migrated-running-status')
        run.report['target_snapshot']=fresh
        assert fresh['omp_isolation']['checked'] and fresh['omp_isolation']['ok'],fresh['omp_isolation']
        assert all(fresh['bridge'][role]['connected'] and fresh['bridge'][role]['pid_matches_pane']
                   for role in ('manager','worker')),fresh['bridge']
        assert fresh['automation']['paused']
        assert not fresh['boot']['confirmation_required'],fresh['boot']
        assert run.provider.requests==1 and not sentinel.exists()
        assert all(hashlib.sha256((target/relative).read_bytes()).hexdigest()==digest for relative,digest in sessions.items())
        record=json.loads((target/'backend.json').read_text())
        assert record['data_dir']==str(target) and record['project_dir']==str(project)
        assert record['project_id']!=old.get('project_id') and record['processes']['backend']['pid']!=old['processes']['backend']['pid']
        assert record['runtime_root']!=str(source) and record['omp_runtime_root']
        run.bound_sockets([record])
        assert os.readlink(target/'omp-root/agent/agent.db')==str(Path(run.env['HOME'])/'.omp/agent/agent.db')
        run.run([run.command,'shutdown','--yes','--json'],'migrated-confirmed-shutdown',cwd=project,timeout=65)
        assert snapshot(source)==before,'source changed during target restart/shutdown'
        (run.output/'source-after.json').write_text(json.dumps(snapshot(source),indent=2))
        run.report['source_session_sha256']=sessions
        run.report['records']=[old,record]
        run.report['target_snapshot']=fresh
        run.report['checks'].update(source_all_bytes_size_mode_mtime_preserved=True,
            actual_OMP_session_history=True,Task_raw_important_fixture_reader_reopen=True,
            inactive_control_and_external_worktree_reference=True,fresh_project_runtime_identity=True,
            pause_boot_boundaries_preserved=True,no_model_turn_or_experiment_replayed=True)
    except BaseException as error:
        run.report['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        run.cleanup()


if __name__=='__main__':main()
