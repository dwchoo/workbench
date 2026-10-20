"""Explicit read-only legacy source -> incomplete private project transaction."""
from __future__ import annotations

from contextlib import ExitStack
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
import uuid

from .boot import read_boot_id
from .paths import DataDirError, DataLayout, InstanceLock, write_private_json, write_private_bytes
from .process_identity import inspect_backend, process_state, read_backend_record
from .project import ensure_project_ignored, project_initialization_locks


_CONTROLS = {'backend.json', 'backend-starting.json', 'backend.lock'}
_KNOWN = _CONTROLS | {'tasks.sqlite3', 'tasks.sqlite3-wal', 'tasks.sqlite3-shm', 'omp-root', 'raw-logs', 'workflow', 'important-logs'}


def _directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_dev, info.st_ino) != (path.lstat().st_dev, path.lstat().st_ino)):
        os.close(fd)
        raise DataDirError(f'migration directory must be private, owned and unchanged: {path}')
    return fd


def _check_binding(path, fd):
    opened,named=os.fstat(fd),path.lstat()
    if (not stat.S_ISDIR(named.st_mode) or named.st_uid!=os.geteuid()
            or stat.S_IMODE(named.st_mode)!=0o700
            or (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino)):
        raise DataDirError(f'migration directory identity changed: {path}')


def _classification(key, info):
    parts=Path(key).parts
    if key.startswith('workflow/worktrees/'):
        return 'external-worktree-reference'
    if key.startswith('omp-root/agent/agent.db') and (parts[-1]=='agent.db' or parts[-1].startswith(('agent.db-','agent.db.moved-'))):
        return 'auth-link-metadata-only'
    caches=('omp-root/cache','omp-root/run','omp-root/bun-transpiler-cache','omp-root/agent/cache',
            'omp-root/agent/terminal-sessions','omp-root/agent/predict','omp-root/agent/models.db','omp-root/agent/skill-descriptions.db')
    if any(key==p or key.startswith(p+'/') or (p.endswith('.db') and key.startswith(p+'-')) for p in caches):
        return 'regenerable-cache'
    if key.startswith('omp-root/agent/agents/'):
        if len(parts)==4 and parts[-1].endswith('.md'):return 'installed-asset'
        raise DataDirError(f'unrecognized installed asset entry: {key}')
    inactive={'backend.json','backend.prev.json','backend-starting.json','backend.lock','boot.json','startup.json','lifecycle.json',
              'lifecycle.json.lock','raw-rotation.json','omp-isolation-manager.yml','omp-isolation-worker.yml',
              'workflow/handoffs.jsonl','workflow/tasks-flow.jsonl','omp-root/agent/config.yml'}
    if key in inactive or parts[0]=='summary-compatibility':return 'inactive-control-history'
    directories={'omp-root','omp-root/agent','omp-root/agent/agents','omp-root/agent/sessions','omp-root/agent/custom-session-files',
                 'omp-root/logs','raw-logs','raw-logs/.status','raw-v2','important-logs','workflow','workflow/runs','workflow/terminal','workflow/worktrees'}
    if key in directories and stat.S_ISDIR(info.st_mode):return 'durable-directory'
    if key in ('tasks.sqlite3','tasks.sqlite3-wal','tasks.sqlite3-journal'):return 'task-database'
    if key in ('records.sqlite3','records.sqlite3-journal'):return 'work-records-database'
    if key=='tasks.sqlite3-shm' or key.endswith(('.db-shm','.sqlite3-shm')):return 'sqlite-rebuild-index'
    if key=='backend.log':return 'backend-diagnostic-history'
    if key=='omp-root/agent/models.yml':return 'custom-model-configuration'
    if key in ('pause.jsonl','automation.json'):return 'pause-history'
    if parts[0]=='raw-logs' and (len(parts)==2 and (parts[-1] in ('.quota.lock','.workbench-log-store') or parts[-1].endswith('.log')) or len(parts)==3 and parts[1]=='.status' and parts[-1].endswith('.json')):return 'legacy-raw'
    if parts[0]=='raw-v2' and len(parts)==2 and (parts[-1] in ('.workbench-segmented-raw-v1','.segment.lock','.control.json') or re.fullmatch(r'seg-[0-9]{16}\.raw',parts[-1])):return 'segmented-raw'
    if parts[0]=='important-logs' and len(parts)==2 and (parts[-1] in ('active.json','maintenance.lock') or re.fullmatch(r'[0-9a-f-]{36}\.sqlite3(?:-wal|-journal)?',parts[-1])):return 'important-database'
    if len(parts)>=3 and parts[:2]==('workflow','runs'):
        if len(parts)==3 and stat.S_ISDIR(info.st_mode):return 'workflow-run'
        if len(parts)==4 and parts[-1] in ('run.json','raw.log','raw-descriptor.json','parent-cwd.txt'):return 'workflow-history'
    if parts[:2]==('workflow','terminal') and len(parts)==3 and parts[-1].endswith(('.log','.json','.sh')):return 'terminal-history'
    if key.startswith(('omp-root/agent/sessions/','omp-root/agent/custom-session-files/')) and (stat.S_ISDIR(info.st_mode) or parts[-1].endswith(('.jsonl','.json'))):return 'omp-session'
    if key=='omp-root/agent/last-changelog-version':return 'omp-history-metadata'
    if key in ('omp-root/agent/history.db','omp-root/agent/history.db-wal','omp-root/agent/history.db-journal'):return 'omp-history-database'
    if parts[:2]==('omp-root','logs') and len(parts)==3 and parts[-1].endswith('.log'):return 'omp-diagnostic-history'
    raise DataDirError(f'unrecognized migration inventory entry: {key}')


def _inventory(fd, prefix=''):
    result = {}
    for name in sorted(os.listdir(fd)):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        key = prefix + name
        item = {'mode':stat.S_IMODE(info.st_mode), 'size':info.st_size, 'mtime_ns':info.st_mtime_ns, 'device':info.st_dev, 'inode':info.st_ino}
        item['classification']=_classification(key,info)
        if info.st_uid != os.geteuid():
            raise DataDirError(f'migration entry has a foreign owner: {key}')
        if item['classification']=='auth-link-metadata-only':
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise DataDirError('migration auth store has an unsupported type')
            item['kind']='auth'
            item['classification']='auth-link-metadata-only'
            if stat.S_ISLNK(info.st_mode):item['link']=os.readlink(name,dir_fd=fd)
        elif stat.S_ISDIR(info.st_mode):
            item['kind'] = 'directory'
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try: result.update(_inventory(child, key + '/'))
            finally: os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            item['kind'] = 'file'
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                digest = sha256()
                while data := os.read(child, 1 << 20): digest.update(data)
                item['sha256'] = digest.hexdigest()
            finally: os.close(child)
        elif stat.S_ISLNK(info.st_mode) and item['classification'] in ('installed-asset','regenerable-cache','external-worktree-reference'):
            item['kind']='link';item['link']=os.readlink(name,dir_fd=fd)
        else:
            raise DataDirError(f'unrecognized migration entry: {key}')
        result[key] = item
    return result


def _copy(source_fd, target_fd, entries, prefix='', directories=None):
    if directories is None:directories={}
    for name in sorted(os.listdir(source_fd)):
        key=prefix+name;item=entries[key]
        if _database_input(key):
            continue  # SQLite inputs live only in anonymous same-filesystem snapshots.
        if item['classification'] in ('inactive-control-history','regenerable-cache','installed-asset',
                                       'external-worktree-reference','sqlite-rebuild-index','auth-link-metadata-only'):
            continue
        if item['kind']=='directory':
            os.mkdir(name,0o700,dir_fd=target_fd)
            src=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=source_fd)
            dst=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=target_fd)
            directories[key]=os.fstat(dst)
            try:_copy(src,dst,entries,key+'/',directories)
            finally:os.close(src);os.close(dst)
        else:_copy_file(source_fd,name,target_fd,name,item)
    os.fsync(target_fd)


def _archive_controls(source_fd,target_fd,entries):
    for key,item in sorted(entries.items()):
        if item['classification']!='inactive-control-history':continue
        src=os.dup(source_fd);dst=os.dup(target_fd)
        try:
            for part in ('migration-history',*Path(key).parts[:-1]):
                try:os.mkdir(part,0o700,dir_fd=dst)
                except FileExistsError:pass
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=dst)
                os.close(dst);dst=child
            name=Path(key).name
            if item['kind']=='directory':
                try:os.mkdir(name,0o700,dir_fd=dst)
                except FileExistsError:pass
            else:
                for part in Path(key).parts[:-1]:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=src)
                    os.close(src);src=child
                _copy_file(src,name,dst,name,item)
            os.fsync(dst)
        finally:os.close(src);os.close(dst)


def _copy_file(source_fd, name, target_fd, target_name, item):
    src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=source_fd)
    dst = os.open(target_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=target_fd)
    try:
        digest = sha256()
        while data := os.read(src, 1 << 20):
            digest.update(data)
            view = memoryview(data)
            while view: view = view[os.write(dst, view):]
        os.fsync(dst)
        if digest.hexdigest() != item['sha256']:
            raise DataDirError(f'source changed during migration copy: {name}')
    finally: os.close(src); os.close(dst)


def _ended(name, ref, uid, boot):
    if (not isinstance(ref,dict) or uid != os.geteuid()
            or any(type(ref.get(k)) is not int or ref[k] <= 0 for k in ('pid','start_ticks'))):
        raise DataDirError(f'migration writer identity is incomplete: {name}')
    try: uuid.UUID(boot)
    except (ValueError,TypeError): raise DataDirError(f'migration writer boot is unknown: {name}')
    state = 'ended' if boot != read_boot_id() and read_boot_id() else process_state(ref['pid'],ref['start_ticks'],uid=uid,boot_id=boot)
    if state != 'ended': raise DataDirError(f'migration writer {name} is {state}; stop its owner first')


def _admission_stopped(layout):
    value=read_backend_record(layout.startup_admission)
    if value is None:return
    info=layout.lock.lstat()
    if (value.get('schema_version')!=1 or value.get('data_dir')!=str(layout.root)
            or value.get('runtime_root')!=str(layout.root) or value.get('project_id') is not None
            or value.get('lock')!={'device':info.st_dev,'inode':info.st_ino}):
        raise DataDirError('migration admission writer binding is unknown')
    for role in ('frontend','backend'):
        _ended('admission_'+role,value.get(role),value.get('uid'),value.get('boot_id'))


def _lifecycle_stopped(layout):
    from dataclasses import asdict
    from workbench.app.lifecycle import LifecycleRecord
    value=read_backend_record(layout.lifecycle_journal)
    if value is None:return
    try:
        if set(value)!={'version','record'} or value['version']!=1:raise ValueError('lifecycle schema')
        record=LifecycleRecord.from_dict(value['record'])
    except (ValueError,TypeError,KeyError) as exc:raise DataDirError('migration lifecycle writer identity is unknown') from exc
    refs=[record.manager.process,record.worker.process,record.shell,*record.run_targets]
    for ref in refs:_ended('lifecycle_'+ref.role,asdict(ref),os.geteuid(),record.boot_marker)


def _shutdown_stopped(record, uid, boot):
    value=record.get('shutdown')
    if value is None:return
    if not isinstance(value,dict):raise DataDirError('migration shutdown writer evidence is unknown')
    groups=[value.get('left_running',[]),value.get('previous_survivors',[])]
    children=value.get('omp_children') or {}
    if not isinstance(children,dict):raise DataDirError('migration shutdown child evidence is unknown')
    groups.extend([children.get('alive',[]),children.get('observed',[])])
    for pane in value.get('panes',[]):
        if not isinstance(pane,dict):raise DataDirError('migration shutdown pane evidence is unknown')
        groups.append(pane.get('survivors',[]))
    for refs in groups:
        if not isinstance(refs,list):raise DataDirError('migration shutdown writer list is unknown')
        for ref in refs:_ended('shutdown_writer',ref,uid,boot)


def _record_uid(record):
    uid = record['uid'] if 'uid' in record else os.geteuid()
    if type(uid) is not int or uid != os.geteuid():
        raise DataDirError('migration writer owner/boot is unknown')
    return uid


def _stopped(layout, lock):
    identity = inspect_backend(layout, reservation=lock)
    record = identity.record
    if identity.state != 'stopped' or record is None:
        raise DataDirError(f'migration requires recorded stopped writers: {layout.root}')
    uid, boot = _record_uid(record), record.get('boot_id')
    if not read_boot_id():
        raise DataDirError('migration writer owner/boot is unknown')
    try: uuid.UUID(boot)
    except (ValueError,TypeError): raise DataDirError('migration writer boot is unknown')
    if not isinstance(record.get('processes'),dict) or not {'backend', 'manager_omp', 'worker_omp', 'host_shell'} <= record['processes'].keys():
        raise DataDirError('migration related writer identity is incomplete')
    for name, ref in record['processes'].items():
        if not isinstance(ref,dict): raise DataDirError(f'migration writer unknown: {name}')
        if any(type(ref.get(k)) is not int or ref[k] <= 0 for k in ('pid', 'start_ticks')):
            raise DataDirError(f'migration writer identity is incomplete: {name}')
        state = 'ended' if boot != read_boot_id() else process_state(ref.get('pid'),ref.get('start_ticks'),uid=uid,boot_id=boot)
        if state != 'ended': raise DataDirError(f'migration writer {name} is {state}; stop its owner first')
    survivors=record.get('survivors')
    if not isinstance(survivors,list):raise DataDirError('migration survivor identity is incomplete')
    for index,ref in enumerate(survivors):
        _ended(f'survivor_{index}',ref,uid,ref.get('boot_id') if isinstance(ref,dict) else None)
    _admission_stopped(layout)
    _lifecycle_stopped(layout)
    _shutdown_stopped(record,uid,boot)
    return record


def _validate_task_schema(connection):
    from workbench.tasks.repository import TaskRepository
    expected=TaskRepository(':memory:')
    try:
        query="SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
        def definitions(database):return [tuple(row) for row in database.execute(query)]
        expected_definitions=definitions(expected._connection)
        if definitions(connection) != expected_definitions:
            raise DataDirError('migration tasks schema definitions differ from supported schema')
        allowed={(kind,name) for kind,name,_,_ in expected_definitions}
        for kind,name in allowed:
            if kind=='table':
                quoted='"'+name.replace('"','""')+'"'
                if list(connection.execute('PRAGMA table_info('+quoted+')')) != [tuple(row) for row in expected._connection.execute('PRAGMA table_info('+quoted+')')]:
                    raise DataDirError(f'migration tasks table schema is unsupported: {name}')
    finally:expected.close()


def _validate_records_schema(connection):
    from workbench.contracts.records_v1 import parse_record
    from workbench.storage.work_records.store import _expected_schema, _SCHEMA_QUERY
    if tuple(connection.execute(_SCHEMA_QUERY)) != _expected_schema():
        raise DataDirError('migration records schema definitions differ from supported schema')
    if connection.execute('SELECT id,kind FROM db_meta').fetchall() != [(1, 'workbench-records-v1')]:
        raise DataDirError('migration records schema identity is unsupported')
    if connection.execute("""SELECT 1 FROM records r LEFT JOIN revisions v USING(record_id)
        GROUP BY r.record_id HAVING COUNT(v.revision)=0 LIMIT 1""").fetchone():
        raise DataDirError('migration record has no saved revision')
    connection.row_factory=sqlite3.Row
    try:
        for row in connection.execute("""SELECT r.record_id,r.created_at,v.revision,v.title,
            v.body,v.task_id,v.recorded_at,v.actor FROM records r JOIN revisions v USING(record_id)"""):
            parse_record(dict(row))
    finally:connection.row_factory=None


def _logical_rows(connection):
    result={}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted='"'+name.replace('"','""')+'"'
        columns=[row[1] for row in connection.execute('PRAGMA table_info('+quoted+')')]
        order=','.join('"'+col.replace('"','""')+'"' for col in columns)
        digest=sha256();count=0
        for row in connection.execute('SELECT * FROM '+quoted+(' ORDER BY '+order if order else '')):
            values=[{'blob':v.hex()} if isinstance(v,bytes) else v for v in row]
            digest.update(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()+b'\n');count+=1
        result[name]={'rows':count,'sha256':digest.hexdigest()}
    return result


def _database_kind(relative):
    if relative=='tasks.sqlite3':return 'tasks'
    if relative=='records.sqlite3':return 'work-records'
    if re.fullmatch(r'important-logs/[0-9a-f-]{36}\.sqlite3',relative):return 'important'
    if relative=='omp-root/agent/history.db':return 'omp-history'
    return None


def _database_input(relative):
    return bool(_database_kind(re.sub(r'-(wal|shm|journal)$','',relative)))


def _same_inode(left,right):
    return (left.st_dev,left.st_ino)==(right.st_dev,right.st_ino)


def _output_parent(root_fd,relative,directories):
    parent=os.dup(root_fd)
    try:
        prefix=[]
        for part in Path(relative).parts[:-1]:
            prefix.append(part);key='/'.join(prefix)
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
            os.close(parent);parent=child
            info=os.fstat(parent)
            if (key not in directories or not _same_inode(info,directories[key])
                    or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700):
                raise DataDirError('migration target database parent identity changed')
        return parent
    except BaseException:
        os.close(parent);raise


def _anonymous_snapshot(source_fd,target_fd,relative,inventory):
    item=inventory[relative];parent=os.dup(source_fd);src=None;snapshot=None
    try:
        prefix=[]
        for part in Path(relative).parts[:-1]:
            prefix.append(part);expected=inventory['/'.join(prefix)]
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
            os.close(parent);parent=child;info=os.fstat(parent)
            if (info.st_dev,info.st_ino)!=(expected['device'],expected['inode']):
                raise DataDirError('migration source database parent identity changed')
        src=os.open(Path(relative).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
        info=os.fstat(src)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                or (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(item['device'],item['inode'],item['size'],item['mtime_ns'])):
            raise DataDirError('migration source database identity changed')
        try:snapshot=os.open('.',os.O_TMPFILE|os.O_RDWR|os.O_CLOEXEC,0o600,dir_fd=target_fd)
        except OSError as exc:
            raise DataDirError('migration target filesystem requires O_TMPFILE anonymous snapshots; choose a supported target filesystem; no named fallback is used') from exc
        digest=sha256();size=0
        while data:=os.read(src,1<<20):
            digest.update(data);size+=len(data);_write_fd(snapshot,data)
        os.fsync(snapshot)
        opened=os.fstat(snapshot)
        if (opened.st_nlink!=0 or opened.st_uid!=os.geteuid() or stat.S_IMODE(opened.st_mode)!=0o600
                or opened.st_dev!=os.fstat(target_fd).st_dev or size!=item['size']
                or digest.hexdigest()!=item['sha256']):
            raise DataDirError('migration anonymous database snapshot differs from inventory')
        source_after=os.fstat(src)
        if (source_after.st_size,source_after.st_mtime_ns)!=(info.st_size,info.st_mtime_ns):
            raise DataDirError('migration source database changed while snapshotting')
        result=snapshot;snapshot=None;return result
    finally:
        if snapshot is not None:os.close(snapshot)
        if src is not None:os.close(src)
        os.close(parent)


def _snapshot_bytes(fd,item):
    size=item['size'];chunks=[];offset=0
    while offset<size:
        data=os.pread(fd,min(1<<20,size-offset),offset)
        if not data:raise DataDirError('migration anonymous snapshot was truncated')
        chunks.append(data);offset+=len(data)
    content=b''.join(chunks);info=os.fstat(fd)
    if (info.st_size!=size or info.st_nlink!=0 or info.st_uid!=os.geteuid()
            or stat.S_IMODE(info.st_mode)!=0o600 or sha256(content).hexdigest()!=item['sha256']):
        raise DataDirError('migration anonymous snapshot identity/content changed')
    return content


def _write_fd(fd,data):
    view=memoryview(data)
    while view:
        written=os.write(fd,view)
        if written<=0:raise OSError('migration write made no progress')
        view=view[written:]


def _publish_database(root_fd,relative,directories,image):
    parent=_output_parent(root_fd,relative,directories);descriptor=None;name=Path(relative).name
    try:
        for suffix in ('-wal','-shm','-journal'):
            try:os.stat(name+suffix,dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:continue
            raise DataDirError('migration target database sidecar collision; inspect incomplete target')
        descriptor=os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
        identity=os.fstat(descriptor);_write_fd(descriptor,image);os.fsync(descriptor)
        named=os.stat(name,dir_fd=parent,follow_symlinks=False)
        if (not _same_inode(identity,named) or not stat.S_ISREG(named.st_mode) or named.st_nlink!=1
                or named.st_uid!=os.geteuid() or stat.S_IMODE(named.st_mode)!=0o600):
            raise DataDirError('migration published database identity changed')
        check=_output_parent(root_fd,relative,directories)
        try:
            if not _same_inode(os.fstat(parent),os.fstat(check)):
                raise DataDirError('migration target database parent changed during publication')
        finally:os.close(check)
        os.fsync(parent)
    finally:
        if descriptor is not None:os.close(descriptor)
        os.close(parent)


def _recover_databases(source_fd,fd,inventory,source_record,directories):
    from .migration_sqlite import committed_image
    verified={}
    for relative,item in inventory.items():
        main=re.sub(r'-(wal|journal)$','',relative)
        if main==relative or _database_kind(main) is None or main in inventory:continue
        if relative.endswith('-journal') and item['size']:
            raise DataDirError('migration nonempty rollback journal is unsupported; recover the original explicitly before migrating: '+relative)
        raise DataDirError('migration recovery companion has a missing main database: '+relative)
    for relative,item in sorted(inventory.items()):
        kind=_database_kind(relative)
        if kind is None:continue
        journal=inventory.get(relative+'-journal')
        if journal and journal['size']:
            raise DataDirError('migration nonempty rollback journal is unsupported; recover the original explicitly before migrating: '+relative)
        with ExitStack() as snapshots:
            main_fd=_anonymous_snapshot(source_fd,fd,relative,inventory);snapshots.callback(os.close,main_fd)
            main=_snapshot_bytes(main_fd,item);wal=b''
            if relative+'-wal' in inventory:
                wal_fd=_anonymous_snapshot(source_fd,fd,relative+'-wal',inventory);snapshots.callback(os.close,wal_fd)
                wal=_snapshot_bytes(wal_fd,inventory[relative+'-wal'])
            image,recovery=committed_image(main,wal)
            connection=sqlite3.connect(':memory:')
            try:
                connection.deserialize(image)
                version=connection.execute('PRAGMA user_version').fetchone()[0]
                if kind=='tasks':
                    if version!=3:raise DataDirError('migration tasks schema is unsupported')
                    _validate_task_schema(connection)
                    source_uid=_record_uid(source_record)
                    for (run,) in connection.execute('SELECT run_id FROM active_runs'):
                        events=connection.execute('SELECT kind,details_json FROM shell_events WHERE run_id=? ORDER BY created_at,event_id',(run,)).fetchall()
                        refs=[json.loads(details) for kind,details in events if kind in ('accepted','started')]
                        for ref in refs:
                            _ended('DB_run_target_'+run,{'pid':ref.get('child_pid'),'start_ticks':ref.get('child_start_ticks')},
                                source_uid,source_record.get('boot_id'))
                        if not refs and any(kind=='sent' for kind,_ in events):raise DataDirError('migration DB run_target sent without exact child identity')
                elif kind=='work-records':
                    if version!=1:raise DataDirError('migration records schema is unsupported')
                    _validate_records_schema(connection)
                elif kind=='omp-history':
                    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    expected={'history','history_fts','history_fts_data','history_fts_idx','history_fts_docsize','history_fts_config','sqlite_sequence'}
                    columns=[row[1] for row in connection.execute('PRAGMA table_info(history)')]
                    if version!=1 or tables!=expected or columns!=['id','prompt','created_at','cwd','session_id','use_count']:
                        raise DataDirError('migration OMP history schema is unsupported')
                else:
                    allowed={'db_meta','entries','subject_stats','important_maintenance_v1','subject_lifecycle_v1','subject_events_v1','subject_commands_v1'}
                    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                    if (version!=1 or not {'db_meta','entries','subject_stats'}<=tables or tables-allowed
                            or connection.execute('SELECT db_uuid FROM db_meta WHERE id=1').fetchone()!=(Path(relative).stem,)):
                        raise DataDirError('migration important schema/identity is unsupported')
                    from workbench.storage.important_log.store import decode_entry_row, _READ_COLUMNS
                    connection.row_factory=sqlite3.Row
                    for row in connection.execute('SELECT '+_READ_COLUMNS+' FROM entries'):decode_entry_row(row)
                    connection.row_factory=None
                if connection.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise DataDirError(f'migration database integrity failed: {relative}')
                if connection.execute('PRAGMA foreign_key_check').fetchall():raise DataDirError(f'migration foreign keys failed: {relative}')
                before=_logical_rows(connection)
                destination=sqlite3.connect(':memory:')
                try:
                    connection.backup(destination)
                    if _logical_rows(destination)!=before:raise DataDirError('migration logical database content changed')
                    if destination.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise DataDirError('migration normalized integrity failed')
                    journal_format=tuple(recovery['journal_format'])
                    if (journal_format not in ((1,1),(2,2)) or (kind=='important' and journal_format!=(2,2))
                            or (kind=='work-records' and journal_format!=(1,1))):
                        raise DataDirError('migration database journal format is unsupported by its public reader')
                    normalized=bytearray(destination.serialize())
                    normalized[18:20]=bytes(journal_format)
                finally:destination.close()
            finally:connection.close()
            _publish_database(fd,relative,directories,normalized)
        verified[relative]={'schema_version':version,'logical_rows':before,'recovery':'anonymous target snapshots, committed WAL image, memory SQLite backup/integrity','wal':recovery}
    return verified


def _verify_important_catalog(fd, inventory, databases):
    if 'important-logs' not in inventory:return None
    from workbench.storage.important_log.control import parse_control,validate_pointer
    root=Path(f'/proc/self/fd/{fd}')
    try:
        raw=(root/'important-logs/active.json').read_bytes()
        if len(raw)>1_000_000:raise ValueError('oversized control')
        pointer=validate_pointer(parse_control(raw))
        expected={pointer['active'],*pointer['archives']}
        present={Path(key).stem for key in databases if key.startswith('important-logs/')}
        if expected!=present:raise ValueError('active/archive store mapping is unavailable')
        # Recovery/backup verified these target copies independently. A source
        # checkpoint status is not a passed check for the new physical copies.
        pointer['archiveChecks']={identity:'complete' for identity in pointer['archives']}
        _bound_write(fd,'important-logs/active.json',pointer)
        return pointer
    except (OSError,ValueError,TypeError,KeyError) as exc:
        raise DataDirError(f'migration important catalog verification failed: {exc}') from exc


def _bound_write(root_fd, relative, value, *, raw=False):
    parts=Path(relative).parts
    parent=os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
            os.close(parent);parent=child
        if raw:write_private_bytes(Path(parts[-1]),value,dir_fd=parent)
        else:write_private_json(Path(parts[-1]),value,dir_fd=parent)
    finally:os.close(parent)


def _rebind_history(fd, source, target, inventory):
    from workbench.storage.log_raw.store import RawLogStore
    from workbench.storage.log_raw.port import raw_query, raw_read_command
    path=Path(f'/proc/self/fd/{fd}')
    dependencies=[{'kind':'source-worktree','path':str(source/key),'accessible':True,'retained_original_reference':True}
                  for key,item in inventory.items() if item['classification']=='external-worktree-reference' and len(Path(key).parts)==3]
    mappings=[]
    for record in sorted((path/'workflow/runs').glob('*/run.json')):
        relative=record.relative_to(path)
        value=json.loads(record.read_bytes())
        run=value.get('run_id')
        if run!=record.parent.name:raise DataDirError('migration workflow run identity mismatch')
        expected=(Path('raw-logs')/f'{run}.log', relative.parent/'raw.log')
        if value.get('log_format')!='segmented-v1':
            selected=next((p for p in expected if value.get('raw_log')==str(source/p)),None)
            if selected is None or str(selected) not in inventory:raise DataDirError('migration raw storage mapping is unavailable')
            value['raw_log']=str(target/selected)
        old_result=value.get('result')
        if old_result is not None:
            if old_result!=str(source/relative):raise DataDirError('migration result storage mapping is unavailable')
            value['result']=str(target/relative)
        if 'read_command' in value:value['read_command']=raw_read_command(target,'run',run)
        if isinstance(value.get('cwd'),str):
            dependencies.append({'kind':'worktree','path':value['cwd'],'accessible':Path(value['cwd']).exists(),'retained_original_reference':True})
        _bound_write(fd,relative,value)
        mappings.append({'record':str(relative),'fields':['raw_log','result','read_command']})
    raw_limits={}
    marker=path/'raw-logs/.workbench-log-store'
    if marker.exists():
        content=marker.read_bytes()
        try:
            limits=tuple(map(int,content.splitlines()[3].split(b':')))
            old_lock=inventory['raw-logs/.quota.lock'];old_status=inventory['raw-logs/.status']
            expected=RawLogStore._marker_content((old_lock['device'],old_lock['inode']),
                (old_status['device'],old_status['inode']),*limits)
            if content!=expected:raise ValueError('marker identity differs')
            new_lock=(path/'raw-logs/.quota.lock').stat();new_status=(path/'raw-logs/.status').stat()
            rebound=RawLogStore._marker_content((new_lock.st_dev,new_lock.st_ino),
                (new_status.st_dev,new_status.st_ino),*limits)
            _bound_write(fd,'raw-logs/.workbench-log-store',rebound,raw=True)
            raw_limits={'per_run_limit':limits[0],'project_limit':limits[1]}
        except (ValueError,IndexError,KeyError,TypeError) as exc:raise DataDirError('migration raw marker mapping failed') from exc
    status_root=path/'raw-logs/.status'
    for status_file in sorted(status_root.glob('*.json')):
        value=json.loads(status_file.read_bytes())
        run=status_file.stem
        decoded=RawLogStore._decode_status(run,value)
        raw=Path('raw-logs')/f'{run}.log'
        original=inventory.get(str(raw))
        if original is None:
            if decoded.raw_identity is not None or decoded.snapshot.stored_bytes:raise DataDirError('migration raw file is missing')
        else:
            if (decoded.raw_identity!=(original['device'],original['inode'])
                    or decoded.snapshot.stored_bytes!=original['size']):raise DataDirError('migration raw identity/size mismatch')
            now=(path/raw).stat(follow_symlinks=False)
            value['raw_identity']=[now.st_dev,now.st_ino]
            _bound_write(fd,status_file.relative_to(path),value)
    control=path/'raw-v2/.control.json'
    if control.exists():
        value=json.loads(control.read_bytes())
        if (not isinstance(value.get('live'),dict) or any(value.get(k) is not None for k in ('intent','eviction','creating'))):
            raise DataDirError('migration segmented raw needs explicit source recovery first')
        for seq,identity in value['live'].items():
            if not isinstance(seq,str) or not seq.isdecimal():raise DataDirError('migration raw segment sequence is invalid')
            relative=f'raw-v2/seg-{int(seq):016d}.raw';original=inventory.get(relative)
            if original is None or identity!=[original['device'],original['inode']]:raise DataDirError('migration segmented raw source identity differs')
            now=(path/relative).stat(follow_symlinks=False)
            value['live'][seq]=[now.st_dev,now.st_ino]
        _bound_write(fd,'raw-v2/.control.json',value)
        from workbench.storage.log_raw.segments import ReadonlySegmentedRawStore
        with ReadonlySegmentedRawStore(path/'raw-v2') as reopened:
            for entry in reopened._control['sources'].values():
                offset=0
                while offset<entry['high']:
                    end=min(offset+128*1024,entry['high'])
                    reopened.read_raw(entry['source'],offset,end);offset=end
    # Reopen using the public exact-ID reader. The anchored staging path differs
    # from its final operational path, so only reader calls use the final name.
    for record in (target/'workflow/runs').glob('*/run.json'):
        result=raw_query(target,'run',record.parent.name)
        if result['status'] not in ('available','complete','partial'):raise DataDirError('migration raw reader verification failed')
    return mappings,dependencies


def _migration_boot(source, record):
    from .boot import BootStore, BootRecordError
    current=read_boot_id();unreadable=False
    try:stored=BootStore(source/'boot.json').read()
    except BootRecordError:stored=None;unreadable=True
    recorded=(stored or {}).get('recorded_boot_id') or record.get('boot_id')
    reason=None
    if not current:reason='boot_marker_unknown'
    elif unreadable:reason='boot_record_unreadable'
    elif stored and stored['pending']:
        reason=stored.get('reason') if stored.get('reason') in ('reboot','boot_marker_unknown','boot_record_unreadable','reconcile_failed') else 'reboot'
    elif not recorded:reason='boot_marker_unknown'
    elif recorded!=current:reason='reboot'
    elif record.get('phase')!='stopped' or (record.get('shutdown') or {}).get('verified') is not True:
        reason='boot_marker_unknown'
    return {'version':1,'recorded_boot_id':recorded,'confirmed_boot_id':None,
            'pending':reason is not None,'pending_boot_id':current if reason else None,
            'reason':reason,'history':[]}


def _publish_marker(target, root_fd, project_fd, marker):
    """Retain the private marker fd through link+fsync and withdraw only our inode."""
    temporary='.migration-project-'+uuid.uuid4().hex
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=root_fd)
    published=False
    try:
        payload=json.dumps(marker,sort_keys=True,separators=(',',':')).encode()
        view=memoryview(payload)
        while view:
            count=os.write(fd,view)
            if count<=0:raise OSError('migration marker write did not progress')
            view=view[count:]
        os.fsync(fd)
        _check_binding(target,root_fd)
        os.link(temporary,'project.json',src_dir_fd=root_fd,dst_dir_fd=root_fd,follow_symlinks=False)
        published=True
        named,opened=os.stat('project.json',dir_fd=root_fd,follow_symlinks=False),os.fstat(fd)
        if (named.st_dev,named.st_ino)!=(opened.st_dev,opened.st_ino):raise DataDirError('migration marker publication identity changed')
        os.fsync(root_fd);_check_binding(target,root_fd);os.fsync(project_fd)
    except BaseException:
        if published:
            try:
                named,opened=os.stat('project.json',dir_fd=root_fd,follow_symlinks=False),os.fstat(fd)
                if (named.st_dev,named.st_ino)==(opened.st_dev,opened.st_ino):
                    os.unlink('project.json',dir_fd=root_fd);os.fsync(root_fd)
            except OSError:pass
        raise
    finally:
        os.close(fd)
        try:os.unlink(temporary,dir_fd=root_fd)
        except FileNotFoundError:pass


def migrate(source: Path, project: Path, environment) -> dict:
    """Copy a known stopped legacy store, preserving source bytes and metadata."""
    source, project = source.expanduser().absolute(), project.expanduser().absolute()
    try:
        if source != source.resolve(strict=True) or project != project.resolve(strict=True):
            raise DataDirError('migration paths must be canonical and cannot traverse symlinks')
    except OSError as exc:raise DataDirError(f'migration source/project path is unavailable: {exc}') from exc
    target = project / '.wb'
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise DataDirError('migration source and target must be different, non-nested paths')
    if os.path.lexists(target): raise DataDirError(f'migration target already exists: {target}')
    try:source_fd = _directory(source)
    except OSError as exc:raise DataDirError(f'migration source directory is unavailable: {source}: {exc}') from exc
    target_fd = None
    lock = InstanceLock(source/'backend.lock')
    try:
        record = read_backend_record(source/'backend.json',dir_fd=source_fd)
        if record is None: raise DataDirError('migration source has no recorded writer identity')
        with ExitStack() as stack:
            project_fds=stack.enter_context(project_initialization_locks((Path(record['project_dir']),project),environment))
            lock.acquire_existing_readonly()
            _check_binding(source,source_fd)
            record=_stopped(DataLayout(source),lock)
            _check_binding(source,source_fd)
            before = _inventory(source_fd)
            from .migration_config import validate_source_configs
            validate_source_configs(source_fd,before)
            if 'tasks.sqlite3' not in before:
                raise DataDirError('migration source inventory/schema is unrecognized')
            if os.path.lexists(target): raise DataDirError(f'migration target already exists: {target}')
            ensure_project_ignored(project)
            os.mkdir('.wb',0o700,dir_fd=project_fds[project])
            target_fd=os.open('.wb',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=project_fds[project])
            os.fsync(project_fds[project])
            phase='initializing_incomplete_target'
            try:
                write_private_json(target/'migration.json',{'schema_version':1,'state':'copying','source':str(source)},dir_fd=target_fd,replace_existing=False)
                # This is a byte snapshot for source-writer preflight, not yet a
                # completed/stopped history migration. SQLite opens only memory images.
                phase='source_database_snapshot'
                task_entries={key:item for key,item in before.items() if key.startswith('tasks.sqlite3')}
                databases=_recover_databases(source_fd,target_fd,task_entries,record,{})
                _check_binding(source,source_fd);lock.verify_reservation(source/'backend.lock')
                record=_stopped(DataLayout(source),lock)
                if _inventory(source_fd)!=before:raise DataDirError('migration source changed during DB-only writer preflight')
                phase='copying_history'
                write_private_json(target/'migration.json',{'schema_version':1,'state':'copying_history','source':str(source),
                    'writers':'recorded/control/DB identities proved ended'},dir_fd=target_fd)
                directories={}
                _copy(source_fd,target_fd,before,directories=directories)
                _archive_controls(source_fd,target_fd,before)
                if 'omp-root/agent/agent.db' in before:
                    from .omp_home import user_auth_store, auth_store_state
                    store=user_auth_store(environment)
                    if not auth_store_state(store).ok:raise DataDirError('migration requires an existing usable user auth store for relinking')
                    agent_fd=os.open('omp-root/agent',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=target_fd)
                    try:os.symlink(str(store),'agent.db',dir_fd=agent_fd)
                    finally:os.close(agent_fd)
                _check_binding(source,source_fd);_check_binding(target,target_fd)
                phase='verifying_history'
                databases.update(_recover_databases(source_fd,target_fd,{key:item for key,item in before.items() if not key.startswith('tasks.sqlite3')},record,directories))
                catalog=_verify_important_catalog(target_fd,before,databases)
                _check_binding(target,target_fd)
                mappings,dependencies=_rebind_history(target_fd,source,target,before)
                if _inventory(source_fd) != before: raise DataDirError('migration source changed')
                lock.verify_reservation(source/'backend.lock')
                _check_binding(source,source_fd);_check_binding(target,target_fd)
                # Reconstruct only verified boot facts, never prior confirmation or
                # active process/approval records. Core owns the next startup check.
                boot_fact=_migration_boot(source,record)
                write_private_json(target/'boot.json',boot_fact,dir_fd=target_fd,replace_existing=False)
                manifest={'schema_version':1,'state':'completed','source':str(source),'target':str(target),'inventory':before,'operational_mappings':mappings,'source_dependencies':dependencies,'databases':databases,'boot_fact':boot_fact,'important_catalog':catalog}
                write_private_json(target/'migration-manifest.json',manifest,dir_fd=target_fd,replace_existing=False)
                _check_binding(source,source_fd)
                if _inventory(source_fd)!=before:raise DataDirError('migration source changed before completion')
                lock.verify_reservation(source/'backend.lock')
                phase='publishing_project'
                project_id=str(uuid.uuid4())
                write_private_json(target/'migration.json',{'schema_version':1,'state':'verified',
                    'source':str(source),'target':str(target),'project_id':project_id},dir_fd=target_fd)
                _publish_marker(target,target_fd,project_fds[project],{'schema_version':1,
                    'project_id':project_id,'project_root':str(project),'data_root':str(target),'state':'completed'})
                return {'state':'completed','source':str(source),'target':str(target),'manifest':str(target/'migration-manifest.json'),'source_preserved':True}
            except BaseException as exc:
                try: write_private_json(target/'migration.json',{'schema_version':1,'state':'failed','phase':phase,'error':type(exc).__name__,'source':str(source)},dir_fd=target_fd)
                except OSError: pass
                raise DataDirError(f'migration incomplete at {target} during {phase}: {type(exc).__name__}: {exc}; inspect this target before retrying') from exc
    except (OSError,KeyError,ValueError,sqlite3.Error) as exc:
        raise DataDirError(f'migration refused/incomplete from {source} to {target}: {exc}') from exc
    finally:
        lock.release()
        if target_fd is not None:os.close(target_fd)
        os.close(source_fd)
