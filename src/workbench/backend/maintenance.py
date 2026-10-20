"""업데이트에서 재사용하는 읽기 전용 현재 사용자 WB 탐색."""
from __future__ import annotations

import os
import fcntl
import stat
from contextlib import contextmanager
import hashlib
from pathlib import Path

from .paths import DataDirError, DataLayout
from .process_identity import inspect_backend, read_backend_record
from .project import resolve_project_context, read_project_marker


FALLBACK_RUNTIME = Path('/tmp')/f'omp-workbench-{os.geteuid()}'
FENCE_BASE = Path('/tmp')/f'omp-workbench-maintenance-{os.geteuid()}'
PROC = Path('/proc')


@contextmanager
def fence(*, shared=False):
    """환경별 runtime 위치와 무관한 사용자 범위 start/update fence."""
    from .runtime_paths import _plain_ancestors
    _plain_ancestors(FENCE_BASE)
    try:
        FENCE_BASE.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = FENCE_BASE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DataDirError('업데이트 fence directory가 안전하지 않습니다.')
    descriptor = os.open(FENCE_BASE/'update.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise DataDirError('업데이트 fence 파일이 안전하지 않습니다.')
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataDirError('업데이트 또는 backend 시작 준비 중입니다. 완료 후 다시 시도하세요.') from exc
        yield descriptor
    finally:
        os.close(descriptor)


def discover(args):
    roots = set()
    processes = {}
    context = resolve_project_context(Path.cwd(), os.environ, args.data_dir)
    if not context.needs_initialization:
        roots.add(context.data_root)
    runtime = os.environ.get('XDG_RUNTIME_DIR', '')
    bases = [FALLBACK_RUNTIME]
    if runtime and os.path.isabs(runtime):
        bases.append(Path(runtime)/'omp-workbench')
    for base in bases:
        if not base.exists():
            continue
        if base.is_symlink() or base.stat().st_uid != os.geteuid():
            raise DataDirError('현재 사용자 runtime namespace를 확인할 수 없습니다.')
        for directory in base.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                raise DataDirError('확인되지 않은 WB runtime 후보가 있습니다.')
            binding = read_backend_record(directory/'runtime.json')
            if not binding or binding.get('schema_version') != 1:
                raise DataDirError('확인되지 않은 WB runtime binding이 있습니다.')
            root = Path(binding['data_root'])
            if not root.is_absolute() or not root.is_dir() or root != root.resolve():
                raise DataDirError('WB runtime의 원본 data root를 확인할 수 없습니다.')
            marker = read_project_marker(root)
            namespace = hashlib.sha256((str(root)+'\0'+marker['project_id']).encode()).hexdigest()[:32]
            if binding.get('project_id') != marker['project_id'] or directory.name != namespace:
                raise DataDirError('WB runtime binding identity가 원본과 다릅니다.')
            roots.add(root)
    # 다른 사용자는 열지 않는다. 현재 사용자 WB bootstrap 후보는 binding이
    # 없거나 startup 중이어도 놓치지 않고 authoritative record와 대조한다.
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            with (entry/'cmdline').open('rb') as stream:
                raw = stream.read((1 << 20)+1)
            if len(raw) > 1 << 20:
                raise DataDirError('현재 사용자 process 인자를 확인할 수 없습니다.')
            argv = raw.split(b'\0')
            if b'_backend' not in argv or not any(b'workbench' in value for value in argv):
                continue
            if b'--data-dir' not in argv:
                raise DataDirError('data root 미확인 WB process가 있습니다.')
            root = Path(os.fsdecode(argv[argv.index(b'--data-dir')+1]))
            if not root.is_absolute() or root != root.resolve():
                raise DataDirError('WB process의 data root identity 미확인')
            roots.add(root)
            processes.setdefault(root, set()).add(int(entry.name))
        except FileNotFoundError:
            continue
        except (OSError, IndexError) as exc:
            raise DataDirError('현재 사용자 WB process 후보를 확인할 수 없습니다.') from exc
    result = []
    for root in sorted(roots):
        identity = inspect_backend(DataLayout(root))
        if identity.state == 'starting':
            raise DataDirError('시작 중이거나 확인되지 않은 backend가 있습니다; 업데이트를 보류합니다.')
        if root in processes and (identity.state != 'running' or
                processes[root] != {identity.record['processes']['backend']['pid']}):
            raise DataDirError('WB process 후보와 backend record가 일치하지 않습니다.')
        result.append(identity)
    return result


def require_no_writers(root):
    """현재 사용자의 열린 descriptor 경로·access flags만 확인한다."""
    root = Path(root)
    for process in PROC.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.geteuid():
                continue
            for descriptor in (process/'fd').iterdir():
                try:
                    value = os.readlink(descriptor)
                    path = Path(value.removesuffix(' (deleted)'))
                    if not path.is_absolute() or not path.is_relative_to(root):
                        continue
                    fields = (process/'fdinfo'/descriptor.name).read_text().splitlines()
                    flags = [int(line.split()[1], 8) for line in fields if line.startswith('flags:')]
                    if len(flags) != 1 or flags[0] & os.O_ACCMODE:
                        raise DataDirError('WB 원본을 쓰는 process가 남아 있거나 access 상태 미확인입니다.')
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DataDirError('WB 원본 writer 부재를 확인할 수 없습니다.') from exc
