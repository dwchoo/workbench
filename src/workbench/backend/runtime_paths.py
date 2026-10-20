"""Private, bounded-length runtime namespaces independent of durable project paths."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Mapping

from .paths import DataDirError, InstanceLock, MAX_SOCKET_PATH_BYTES, read_private_json, write_private_json


def _plain_ancestors(path: Path) -> None:
    if not path.is_absolute():
        raise DataDirError(f'runtime path must be absolute: {path}')
    for ancestor in path.parents:
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise DataDirError(f'runtime ancestor is not a plain directory: {ancestor}')


def _private_existing(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise DataDirError(f'runtime path must be a private user-owned plain directory: {path}')
    return True


def _binding(data_root: Path, project_id: str) -> dict[str, str | int]:
    return {'schema_version': 1, 'data_root': str(Path(data_root).resolve()), 'project_id': project_id}


def _validate_binding(runtime_root: Path, data_root: Path, project_id: str) -> None:
    marker = runtime_root / 'runtime.json'
    try:
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DataDirError(f'runtime identity collision or invalid marker: {runtime_root}')
        value = read_private_json(marker)
    except OSError as exc:
        raise DataDirError(f'runtime identity collision or invalid marker: {runtime_root}: {exc}') from exc
    if value != _binding(data_root, project_id):
        raise DataDirError(f'runtime identity collision or invalid marker: {runtime_root}')


def resolve_runtime_root(data_root: Path, project_id: str,
                         environment: Mapping[str, str]) -> Path:
    """Select and validate without creating anything; never silently accept unsafe paths."""
    namespace = hashlib.sha256((str(Path(data_root).resolve()) + '\0' + project_id).encode()).hexdigest()[:32]
    selected = environment.get('XDG_RUNTIME_DIR', '')
    if selected and os.path.isabs(selected):
        _plain_ancestors(Path(selected))
    if selected and os.path.isabs(selected) and _private_existing(Path(selected)):
        base = Path(selected) / 'omp-workbench'
        candidate = base / namespace
        if len(os.fsencode(candidate / 'bridge.sock')) <= MAX_SOCKET_PATH_BYTES:
            _private_existing(base)
            if _private_existing(candidate):
                _validate_binding(candidate, data_root, project_id)
            return candidate
    base = Path('/tmp') / f'omp-workbench-{os.geteuid()}'
    candidate = base / namespace
    _private_existing(base)
    if _private_existing(candidate):
        _validate_binding(candidate, data_root, project_id)
    if len(os.fsencode(candidate / 'bridge.sock')) > MAX_SOCKET_PATH_BYTES:
        raise DataDirError(f'runtime socket path is too long: {candidate}')
    return candidate


def ensure_runtime_root(runtime_root: Path, data_root: Path, project_id: str) -> Path:
    """Prepare selected runtime and refuse an existing conflicting identity."""
    runtime_root = Path(runtime_root)
    _plain_ancestors(runtime_root)
    if len(os.fsencode(runtime_root / 'bridge.sock')) > MAX_SOCKET_PATH_BYTES:
        raise DataDirError(f'runtime socket path is too long: {runtime_root}')
    for path in (runtime_root.parent, runtime_root):
        if not _private_existing(path):
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            _private_existing(path)
    lock = InstanceLock(runtime_root / '.identity.lock')
    lock.acquire()
    try:
        marker = runtime_root / 'runtime.json'
        expected = _binding(data_root, project_id)
        if os.path.lexists(marker):
            _validate_binding(runtime_root, data_root, project_id)
        else:
            if any(path.name != '.identity.lock' for path in runtime_root.iterdir()):
                raise DataDirError(f'runtime directory has unbound contents: {runtime_root}')
            write_private_json(marker, expected)
    finally:
        lock.release()
    return runtime_root
