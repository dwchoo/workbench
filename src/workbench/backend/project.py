"""Side-effect free project discovery and explicitly confirmed initialization."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
import json
import stat
import subprocess
import fcntl
import uuid
from pathlib import Path
from typing import Callable, Mapping

from .paths import DataDirError, write_private_json, read_private_json
from .runtime_paths import resolve_runtime_root


@dataclass(frozen=True, slots=True)
class ProjectContext:
    project_root: Path
    data_root: Path
    runtime_root: Path
    source: str
    project_id: str | None
    needs_initialization: bool


def read_project_marker(data_root: Path) -> dict:
    """Validate a completed marker without changing permissions or following symlinks."""
    data_root = Path(data_root)
    try:
        info = data_root.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise DataDirError(f'symlink project directory is not allowed: {data_root}')
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise DataDirError(f'project directory must be private and user-owned: {data_root}')
        descriptor = os.open(data_root / 'project.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
                raise ValueError('marker must be a private regular file')
            marker = json.loads(os.read(descriptor, 65537))
        finally:
            os.close(descriptor)
        if (not isinstance(marker, dict) or marker.get('schema_version') != 1
                or marker.get('state') != 'completed'
                or marker.get('project_root') != str(data_root.parent.resolve())
                or marker.get('data_root',str(data_root.resolve())) != str(data_root.resolve())
                or str(uuid.UUID(marker.get('project_id', ''))) != marker.get('project_id')):
            raise ValueError('incomplete, unsupported or mismatched identity')
        if os.path.lexists(data_root / 'migration.json'):
            migration=read_private_json(data_root/'migration.json',65536)
            if (not isinstance(migration,dict) or migration.get('schema_version')!=1
                    or migration.get('state')!='verified' or migration.get('target')!=str(data_root.resolve())
                    or migration.get('project_id')!=marker['project_id']):
                raise ValueError('migration is incomplete or completion identity differs')
        return marker
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise DataDirError(f'invalid project marker at {data_root}: {exc}') from exc


def resolve_project_context(cwd: Path, environment: Mapping[str, str],
                            cli_data_dir: str | Path | None = None) -> ProjectContext:
    """Resolve flag/env overrides or nearest marker without writing any files.

    A missing project is returned as a proposal; only initialize_project may publish it.
    Legacy explicit stores retain their in-place sockets until explicitly migrated.
    """
    root = Path(cwd).resolve(strict=True)
    selected = cli_data_dir or environment.get('WORKBENCH_DATA_DIR')
    if selected:
        data = Path(selected).expanduser()
        if not data.is_absolute():
            data = root / data
        if data.is_symlink():
            raise DataDirError(f'symlink data directory is not allowed: {data}')
        data = data.resolve()
        source = 'explicit' if cli_data_dir else 'environment'
        if os.path.lexists(data / 'project.json') or os.path.lexists(data / 'migration.json'):
            marker = read_project_marker(data)
            return ProjectContext(data.parent, data, resolve_runtime_root(data, marker['project_id'], environment),
                                  source, marker['project_id'], False)
        return ProjectContext(root, data, data, source, None, False)
    for ancestor in (root, *root.parents):
        data = ancestor / '.wb'
        if os.path.lexists(data):
            marker = read_project_marker(data)
            return ProjectContext(ancestor, data, resolve_runtime_root(data, marker['project_id'], environment),
                                  'project', marker['project_id'], False)
    return ProjectContext(root, root / '.wb', resolve_runtime_root(root / '.wb', 'uninitialized', environment),
                          'project', None, True)


def _git(project_root: Path, *arguments: str) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    try:
        return subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(project_root), *arguments],
                              env=environment, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DataDirError(f'cannot inspect Git state for {project_root}: {exc}') from exc


def ensure_project_ignored(project_root: Path) -> None:
    """Preserve existing ignore bytes; reject tracked state rather than untracking it."""
    if not any(os.path.lexists(parent / '.git') for parent in (project_root, *project_root.parents)):
        return
    inside = _git(project_root, 'rev-parse', '--is-inside-work-tree')
    if inside.returncode or inside.stdout.strip() != b'true':
        raise DataDirError(f'cannot inspect Git repository for {project_root}: {inside.stderr.decode(errors="replace")}')
    tracked = _git(project_root, 'ls-files', '-z', '--', '.wb')
    if tracked.returncode or tracked.stdout:
        raise DataDirError(f'project state is already tracked or Git tracking is unknown: {project_root / ".wb"}')
    ignored = _git(project_root, 'check-ignore', '-q', '--no-index', '--', '.wb/')
    if ignored.returncode == 0:
        return
    if ignored.returncode != 1:
        raise DataDirError(f'cannot verify Git ignore for {project_root / ".wb"}')
    path = project_root / '.gitignore'
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o644)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or not info.st_mode & 0o200):
                raise DataDirError(f'Git ignore is not a writable user-owned regular file: {path}')
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            size = os.lseek(descriptor, 0, os.SEEK_END)
            last = os.pread(descriptor, 1, size - 1) if size else b''
            os.write(descriptor, (b'\n' if size and last != b'\n' else b'') + b'/.wb/\n')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise DataDirError(f'cannot apply Git ignore at {path}: {exc}') from exc
    if _git(project_root, 'check-ignore', '-q', '--no-index', '--', '.wb/').returncode:
        raise DataDirError(f'Git ignore did not exclude {project_root / ".wb"}')


@contextmanager
def project_initialization_locks(project_roots, environment: Mapping[str, str] | None = None):
    """Reserve a union of init/start directory locks without self-conflicting ancestors."""
    roots={Path(root) for root in project_roots}
    for root in roots:
        if root!=root.resolve(strict=True):
            raise DataDirError(f'project lock requires a canonical root: {root}')
    paths=set(roots)
    for root in roots:paths.update(root.parents)
    descriptors={}
    try:
        for path in sorted(paths,key=lambda p:(len(p.parts),str(p))):
            fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            descriptors[path]=fd
            fcntl.flock(fd,(fcntl.LOCK_EX if path in roots else fcntl.LOCK_SH)|fcntl.LOCK_NB)
    except OSError as exc:
        for fd in reversed(list(descriptors.values())):os.close(fd)
        raise DataDirError(f'project initialization/migration is locked or unavailable: {sorted(map(str,roots))}: {exc}') from exc
    try:yield descriptors
    finally:
        for fd in reversed(list(descriptors.values())):
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


@contextmanager
def project_initialization_lock(project_root: Path, environment: Mapping[str, str] | None = None):
    """Common init/start/migration boundary, including shared ancestor reservations."""
    with project_initialization_locks((project_root,),environment):yield


def initialize_project(context: ProjectContext, *, confirm: Callable[[Path], bool],
                       interactive: bool, environment: Mapping[str, str],
                       prerequisites: Callable[[], None] | None = None) -> ProjectContext:
    """Confirm the displayed canonical folder, check prerequisites, then publish.

    confirm receives the folder the UI must display and must return an affirmative bool.
    The caller supplies the actual TTY check. Exceptions and refusal create no state.
    """
    if (context.source != 'project' or context.data_root != context.project_root / '.wb'
            or context.project_root != context.project_root.resolve(strict=True)):
        raise DataDirError(f'project initialization context is inconsistent: {context.project_root}')
    if not interactive or not confirm(context.project_root):
        raise DataDirError(f'project initialization requires interactive confirmation: {context.project_root}')
    if prerequisites is not None:
        prerequisites()
    with project_initialization_lock(context.project_root, environment):
        current = resolve_project_context(context.project_root, environment)
        if not current.needs_initialization:
            return current
        ensure_project_ignored(context.project_root)
        context.data_root.mkdir(mode=0o700)
        try:
            directory_fd = os.open(context.data_root,
                                   os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(directory_fd)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise DataDirError(f'project directory must be private and user-owned: {context.data_root}')
                project_id = str(uuid.uuid4())
                write_private_json(context.data_root / 'project.json', {
                    'schema_version': 1, 'project_id': project_id,
                    'project_root': str(context.project_root), 'state': 'completed',
                }, dir_fd=directory_fd, replace_existing=False)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise DataDirError(f'cannot publish initial project marker at {context.data_root}: {exc}') from exc
        return resolve_project_context(context.project_root, environment)
