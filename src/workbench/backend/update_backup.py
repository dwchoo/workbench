"""종료·lock 확인 뒤 WB 원본을 열어 수정하지 않는 private snapshot."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat

from .migration_sqlite import committed_image
from .paths import DataDirError


CACHES = ('omp-root/cache', 'omp-root/run', 'omp-root/bun-transpiler-cache',
          'omp-root/agent/cache', 'omp-root/agent/terminal-sessions', 'omp-root/agent/predict',
          'omp-root/agent/models.db', 'omp-root/agent/skill-descriptions.db', 'workflow/worktrees')


def excluded(key):
    path = Path(key)
    name = path.name.lower()
    if name.startswith(('agent.db', 'auth.db', 'credentials.db', 'auth.json', 'credentials.json')):
        return 'credential (내용·hash 미수집)'
    if any(key == prefix or key.startswith(prefix+'/') or
           (prefix.endswith('.db') and key.startswith(prefix+'-')) for prefix in CACHES):
        return 'regenerable cache / project worktree reference'
    return None


def inventory(root_fd):
    result, exclusions = {}, []
    def visit(directory, prefix=''):
        for name in sorted(os.listdir(directory)):
            key = prefix+name
            reason = excluded(key)
            if reason:
                exclusions.append({'path':key, 'reason':reason})
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if info.st_uid != os.geteuid():
                raise DataDirError('WB snapshot에 다른 소유자의 항목이 있습니다.')
            if stat.S_ISLNK(info.st_mode):
                if key in ('tasks.sqlite3', 'records.sqlite3', 'omp-root', 'omp-root/agent', 'omp-root/agent/sessions'):
                    raise DataDirError('필수 WB 데이터가 symlink여서 확인할 수 없습니다.')
                exclusions.append({'path':key, 'reason':'symlink (대상 미수집)'})
                continue
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise DataDirError('WB snapshot directory identity가 변경되었습니다.')
                    result[key] = ('directory', info.st_dev, info.st_ino, info.st_mtime_ns)
                    visit(child, key+'/')
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                result[key] = ('file', info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            elif not stat.S_ISSOCK(info.st_mode):
                raise DataDirError('WB snapshot에 지원하지 않는 파일 종류가 있습니다.')
            else:
                exclusions.append({'path':key, 'reason':'runtime socket'})
    visit(root_fd)
    return result, exclusions


def source_file(root_fd, key, before):
    parent = os.dup(root_fd)
    prefix = ''
    try:
        for part in Path(key).parts[:-1]:
            prefix += part
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            os.close(parent)
            parent = child
            info = os.fstat(parent)
            if ('directory', info.st_dev, info.st_ino, info.st_mtime_ns) != before[prefix]:
                raise DataDirError('WB backup parent identity가 변경되었습니다.')
            prefix += '/'
        return os.open(Path(key).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)


def snapshot(root, target):
    root = Path(root)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(root_fd)
        if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
            raise DataDirError('WB backup source identity를 확인할 수 없습니다.')
        before, exclusions = inventory(root_fd)
        target.mkdir(mode=0o700, parents=True)
        files = []
        for key, expected in before.items():
            destination = target/key
            if expected[0] == 'directory':
                destination.mkdir(mode=0o700)
                continue
            descriptor = source_file(root_fd, key, before)
            try:
                info = os.fstat(descriptor)
                if ('file', info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != expected or not stat.S_ISREG(info.st_mode):
                    raise DataDirError('WB backup source가 변경되었습니다.')
                output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                digest, count = hashlib.sha256(), 0
                with os.fdopen(output, 'wb') as stream:
                    while block := os.read(descriptor, min(1 << 20, info.st_size-count+1)):
                        count += len(block)
                        if count > info.st_size:
                            raise DataDirError('WB backup source 크기가 변경되었습니다.')
                        digest.update(block)
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                if count != info.st_size:
                    raise DataDirError('WB backup 파일이 불완전합니다.')
                files.append({'path':key, 'sha256':digest.hexdigest(), 'bytes':count})
            finally:
                os.close(descriptor)
        named = root.lstat()
        if ((opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or stat.S_ISLNK(named.st_mode) or inventory(root_fd)[0] != before):
            raise DataDirError('WB backup 중 source inventory가 변경되었습니다.')
    finally:
        os.close(root_fd)
    # SQLite는 복사한 고정 bytes에서만 검사한다. WAL을 반영한 in-memory
    # image를 사용하므로 원본/backup에 SQLite sidecar를 만들지 않는다.
    for entry in files:
        key = entry['path']
        if key.endswith(('.sqlite3', '.db')):
            database = target/key
            wal = Path(str(database)+'-wal')
            journal = Path(str(database)+'-journal')
            if journal.exists() and journal.stat().st_size:
                raise DataDirError('SQLite rollback journal 상태가 미확인입니다.')
            image, _ = committed_image(database.read_bytes(), wal.read_bytes() if wal.exists() else b'')
            connection = sqlite3.connect(':memory:')
            try:
                connection.deserialize(image)
                if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise DataDirError('WB backup SQLite 검증 실패')
            finally:
                connection.close()
    return {'files':files, 'excluded':exclusions}
