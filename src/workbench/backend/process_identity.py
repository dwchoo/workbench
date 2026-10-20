"""Read-only durable backend identity checks shared by CLI and stopped-state consumers.

Unknown identities never authorize attach, another start or a cleanup signal.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from hashlib import sha256
import os
from pathlib import Path
import stat

from .boot import read_boot_id
from .paths import DataDirError, DataLayout, InstanceLock


class BackendIdentityError(DataDirError):
    def __init__(self, reason: str, root: Path):
        self.reason, self.root = reason, root
        super().__init__(f'backend identity {reason}: {root}; inspect the record and stop its owner before retrying')


def read_backend_record(path: Path, *, dir_fd: int | None = None) -> dict | None:
    try:
        descriptor = os.open(path if dir_fd is None else path.name,
                             os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackendIdentityError("record_unavailable", path.parent) from exc
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1 << 20):
            raise BackendIdentityError('record_unsafe', path.parent)
        value = json.loads(os.read(descriptor, (1 << 20) + 1))
        if not isinstance(value, dict):
            raise ValueError('record is not an object')
        return value
    except (ValueError, OSError) as exc:
        raise BackendIdentityError('record_unreadable', path.parent) from exc
    finally:
        os.close(descriptor)


def process_state(pid: int, start_ticks: int, *, uid: int, boot_id: str) -> str:
    """Return alive/ended/unknown from exact UID, PID, startticks and boot; no signal."""
    if (type(pid) is not int or pid <= 0 or type(start_ticks) is not int or start_ticks <= 0
            or type(uid) is not int or uid != os.geteuid() or not boot_id or read_boot_id() != boot_id):
        return 'unknown'
    try:
        process = Path('/proc') / str(pid)
        if process.stat().st_uid != uid:
            return 'unknown'
        fields = (process / 'stat').read_bytes().rsplit(b') ', 1)[1].split()
        if int(fields[19]) != start_ticks:
            return 'unknown'
        return 'ended' if fields[0] in (b'Z', b'X') else 'alive'
    except FileNotFoundError:
        return 'ended'
    except (OSError, IndexError, ValueError):
        return 'unknown'


@dataclass(frozen=True)
class BackendIdentity:
    state: str  # stopped | starting | running
    layout: DataLayout
    project_root: Path | None
    project_id: str | None
    record: dict | None


def _record_digest(record: dict | None) -> str | None:
    return None if record is None else sha256(json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _root_descriptor(root: Path) -> int:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info, named = os.fstat(descriptor), root.lstat()
        if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise BackendIdentityError('startup_root_unsafe', root)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _admission_fields(value: dict, layout: DataLayout, project: str, project_id: str | None) -> bool:
    runtime = value.get('runtime_root')
    nonce = value.get('startup_id')
    return (value.get('schema_version') == 1 and value.get('data_dir') == str(layout.root)
            and (project is None or value.get('project_dir') == project) and value.get('project_id') == project_id
            and isinstance(value.get('project_dir'), str) and Path(value['project_dir']).is_dir()
            and str(Path(value['project_dir']).resolve()) == value['project_dir']
            and value.get('uid') == os.geteuid() and isinstance(value.get('boot_id'), str)
            and bool(value['boot_id']) and isinstance(runtime, str) and Path(runtime).is_absolute()
            and str(Path(runtime).resolve()) == runtime and isinstance(nonce, str) and len(nonce) == 32
            and all(char in '0123456789abcdef' for char in nonce)
            and isinstance(value.get('backend'), dict) and isinstance(value.get('frontend'), dict))


def _admitted_starting(layout: DataLayout, record: dict | None, project: str,
                       project_id: str | None, held: bool) -> bool:
    if not os.path.lexists(layout.startup_admission):
        return False
    try:
        descriptor = _root_descriptor(layout.root)
        try:
            value = read_backend_record(layout.startup_admission, dir_fd=descriptor)
            if value is None:
                return False
            if not _admission_fields(value, layout, project, project_id):
                raise BackendIdentityError('startup_admission_mismatch', layout.root)
            if value['boot_id'] != read_boot_id():
                return False  # Historical control metadata cannot admit a current process.
            lock_info = os.stat(layout.lock.name, dir_fd=descriptor, follow_symlinks=False)
            if value.get('lock') != {'device': lock_info.st_dev, 'inode': lock_info.st_ino}:
                raise BackendIdentityError('startup_lock_inode_mismatch', layout.root)
            ref = value['backend']
            state = process_state(ref.get('pid'), ref.get('start_ticks'), uid=value['uid'], boot_id=value['boot_id'])
            if state == 'ended':
                return False
            if state != 'alive':
                raise BackendIdentityError('startup_process_unknown', layout.root)
            if value.get('previous_record_sha256') != _record_digest(record):
                raise BackendIdentityError('startup_previous_record_mismatch', layout.root)
            # Bind consumption to the same validated durable directory inode as the
            # admission read; a renamed/replaced directory cannot supply another record.
            current = read_backend_record(layout.record, dir_fd=descriptor)
            if _record_digest(current) != _record_digest(record):
                # The admitted child may atomically publish between these reads.
                # This only permits a bounded retry, never contacting that endpoint.
                expected = {key: value[key] for key in
                            ('data_dir', 'project_dir', 'project_id', 'runtime_root', 'uid', 'boot_id')}
                if (current is None or any(current.get(key) != item for key, item in expected.items())
                        or not isinstance(current.get('processes'), dict)
                        or not isinstance(current['processes'].get('backend'), dict)
                        or any(current['processes']['backend'].get(key) != ref.get(key)
                               for key in ('pid', 'start_ticks'))):
                    raise BackendIdentityError('startup_record_changed', layout.root)
            named, opened = layout.root.lstat(), os.fstat(descriptor)
            if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise BackendIdentityError('startup_root_changed', layout.root)
            if held:
                args = (Path('/proc') / str(ref['pid']) / 'cmdline').read_bytes().split(b'\0')
                token = b'--startup-id'
                if (not InstanceLock(layout.lock).held_by(ref['pid']) or token not in args
                        or args[args.index(token) + 1] != value['startup_id'].encode()):
                    raise BackendIdentityError('startup_lock_owner_mismatch', layout.root)
            else:
                parent = value['frontend']
                if process_state(parent.get('pid'), parent.get('start_ticks'), uid=value['uid'],
                                 boot_id=value['boot_id']) != 'alive':
                    raise BackendIdentityError('startup_frontend_unknown', layout.root)
            return True
        finally:
            os.close(descriptor)
    except BackendIdentityError:
        raise
    except (OSError, ValueError, IndexError) as exc:
        raise BackendIdentityError('startup_admission_unavailable', layout.root) from exc


def publish_startup_admission(layout: DataLayout, project: str, pid: int, startup_id: str) -> None:
    """Admit one gated child under the caller's project initialization lock.

    The gate is still closed: preserve the previous backend record for core reconcile.
    Publish no-clobber through the validated directory fd, never through a changed root.
    """
    from .paths import write_private_json
    identity = inspect_backend(layout)
    if identity.state != 'stopped':
        raise BackendIdentityError('startup_not_stopped', layout.root)
    project_id = identity.project_id
    if os.path.lexists(layout.root / 'project.json'):
        from .project import read_project_marker
        project_id = read_project_marker(layout.root)['project_id']
    descriptor = _root_descriptor(layout.root)
    try:
        old = read_backend_record(layout.startup_admission, dir_fd=descriptor)
        if old is not None:
            if not _admission_fields(old, layout, project, project_id):
                raise BackendIdentityError('startup_admission_mismatch', layout.root)
            ref = old['backend']
            if old['boot_id'] == read_boot_id() and process_state(ref.get('pid'), ref.get('start_ticks'),
                    uid=old['uid'], boot_id=old['boot_id']) != 'ended':
                raise BackendIdentityError('startup_admission_not_ended', layout.root)
            os.unlink(layout.startup_admission.name, dir_fd=descriptor)
        lock_fd = os.open(layout.lock.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=descriptor)
        try:
            lock_info = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid()
                    or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_nlink != 1):
                raise BackendIdentityError('startup_lock_unsafe', layout.root)
        finally:
            os.close(lock_fd)
        def reference(number):
            fields = Path(f'/proc/{number}/stat').read_bytes().rsplit(b') ', 1)[1].split()
            return {'pid': number, 'start_ticks': int(fields[19])}
        value = {'schema_version': 1, 'startup_id': startup_id, 'data_dir': str(layout.root),
                 'project_dir': project, 'project_id': project_id, 'runtime_root': str(layout.runtime_root or layout.root),
                 'uid': os.geteuid(), 'boot_id': read_boot_id(), 'frontend': reference(os.getpid()),
                 'backend': reference(pid), 'lock': {'device': lock_info.st_dev, 'inode': lock_info.st_ino},
                 'previous_record_sha256': _record_digest(identity.record)}
        if not value['boot_id']:
            raise BackendIdentityError('startup_boot_unknown', layout.root)
        write_private_json(layout.startup_admission, value, dir_fd=descriptor, replace_existing=False)
        named, opened = layout.root.lstat(), os.fstat(descriptor)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise BackendIdentityError('startup_root_changed', layout.root)
    except OSError as exc:
        raise BackendIdentityError('startup_admission_publish_failed', layout.root) from exc
    finally:
        os.close(descriptor)


def inspect_backend(layout: DataLayout, *, reservation: InstanceLock | None = None) -> BackendIdentity:
    """Validate durable evidence before contacting any endpoint or deciding a new start."""
    record = read_backend_record(layout.record)
    try:
        try:
            lock_info = layout.lock.lstat()
        except FileNotFoundError:
            held = False
        else:
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid()
                    or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_nlink != 1):
                raise BackendIdentityError('lock_unsafe', layout.root)
            if reservation is not None:
                reservation.verify_reservation(layout.lock)
                held = False
            else:
                held = InstanceLock(layout.lock).held_elsewhere()
    except OSError as exc:
        raise BackendIdentityError('lock_unavailable', layout.root) from exc
    if record is None:
        project, project_id = None, None
        if os.path.lexists(layout.root / 'project.json'):
            from .project import read_project_marker
            marker = read_project_marker(layout.root)
            project, project_id = marker['project_root'], marker['project_id']
        admitted = (layout.root.exists() and _admitted_starting(layout, None, project, project_id, held))
        return BackendIdentity('starting' if held or admitted else 'stopped', layout, None, None, None)
    if record.get('data_dir') != str(layout.root.resolve()):
        raise BackendIdentityError('data_root_mismatch', layout.root)
    project = record.get('project_dir')
    if (not isinstance(project, str) or not Path(project).is_absolute()
            or not Path(project).is_dir() or str(Path(project).resolve()) != project):
        raise BackendIdentityError('project_root_mismatch', layout.root)
    project_id = None
    if os.path.lexists(layout.root / 'project.json'):
        from .project import read_project_marker
        marker = read_project_marker(layout.root)
        project_id = marker['project_id']
        if record.get('project_id') != project_id or project != marker['project_root']:
            raise BackendIdentityError('project_marker_mismatch', layout.root)
    runtime = record.get('runtime_root', str(layout.root))
    if (not isinstance(runtime, str) or not Path(runtime).is_absolute()
            or str(Path(runtime).resolve()) != runtime
            or (project_id is None and runtime != str(layout.root))
            or record.get('ui_socket') != str(Path(runtime) / 'ui.sock')
            or record.get('bridge_socket', str(Path(runtime) / 'bridge.sock')) != str(Path(runtime) / 'bridge.sock')):
        raise BackendIdentityError('runtime_endpoint_mismatch', layout.root)
    uid = record.get('uid', os.geteuid())
    boot = record.get('boot_id')
    if type(uid) is not int or uid != os.geteuid() or not boot or read_boot_id() is None:
        raise BackendIdentityError('owner_or_boot_unknown', layout.root)
    if boot != read_boot_id():
        if _admitted_starting(layout, record, project, project_id, held):
            return BackendIdentity('starting', layout, Path(project), project_id, record)
        if held:
            raise BackendIdentityError('boot_mismatch_with_lock', layout.root)
        return BackendIdentity('stopped', layout, Path(project), record.get('project_id'), record)
    processes = record.get('processes')
    if not isinstance(processes, dict) or not isinstance(processes.get('backend'), dict):
        raise BackendIdentityError('process_unrecorded', layout.root)
    reference = processes['backend']
    state = process_state(reference.get('pid'), reference.get('start_ticks'), uid=uid, boot_id=boot)
    if state == 'ended' and _admitted_starting(layout, record, project, project_id, held):
        return BackendIdentity('starting', layout, Path(project), project_id, record)
    if state == 'unknown' or (state == 'ended' and held) or (state == 'alive' and not held):
        raise BackendIdentityError('process_mismatch_or_unknown', layout.root)
    selected = layout
    if state == 'alive':
        runtime_path = Path(runtime)
        for path in (runtime_path, *runtime_path.parents):
            if path.is_symlink():
                raise BackendIdentityError('runtime_symlink', layout.root)
        try:
            info = runtime_path.stat()
        except OSError as exc:
            raise BackendIdentityError('runtime_unavailable', layout.root) from exc
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise BackendIdentityError('runtime_not_private', layout.root)
        if project_id is not None:
            binding = read_backend_record(runtime_path / 'runtime.json')
            if binding != {'schema_version': 1, 'data_root': str(layout.root), 'project_id': project_id}:
                raise BackendIdentityError('runtime_binding_mismatch', layout.root)
        selected = DataLayout(layout.root, runtime_root=runtime_path)
        selected.check_socket_paths()
    return BackendIdentity('running' if state == 'alive' else 'stopped', selected,
                           Path(project), project_id, record)
