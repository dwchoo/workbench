"""Create a one-way Markdown snapshot of a saved work-record revision."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Mapping

from workbench.contracts.records_v1 import RecordContractError, parse_record


def _markdown(record: Mapping[str, object]) -> bytes:
    title = record["title"]
    lines = [
        f"# {title}",
        "",
        f"- Record ID: `{record['record_id']}`",
        f"- Revision: {record['revision']}",
        f"- Created at: {record['created_at']}",
        f"- Recorded at: {record['recorded_at']}",
        f"- Actor: {record['actor']}",
    ]
    if record["task_id"] is not None:
        lines.append(f"- Task ID: `{record['task_id']}`")
    prefix = ("\n".join(lines) + "\n\n---\n\n").encode("utf-8")
    return prefix + record["body"].encode("utf-8")


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def export_record(record: Mapping[str, object], destination: Path) -> dict:
    """Write a validated RecordStore snapshot to an explicit absolute path.

    The caller resolves relative user paths before calling this function. Existing
    leaves are never replaced. A write failure after exclusive creation retains the
    path and reports cleanup as unknown because stat-then-unlink cannot be made
    ownership-safe if another same-user process replaces the directory entry.
    """
    try:
        snapshot = parse_record(dict(record))
        path = Path(destination)
        if not path.is_absolute() or "\0" in os.fspath(path):
            return {"status": "rejected", "reason": "invalid_output_path"}
        payload = _markdown(snapshot)
    except (RecordContractError, TypeError, ValueError, UnicodeError):
        return {"status": "rejected", "reason": "invalid_argument"}

    result_path = str(path)
    directory_fd = None
    file_fd = None
    identity = None
    created_file = False
    try:
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        file_fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        created_file = True
        created = os.fstat(file_fd)
        identity = (created.st_dev, created.st_ino)
        os.fchmod(file_fd, 0o600)
        _write_all(file_fd, payload)
        os.fsync(file_fd)
        visible = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        final = os.fstat(file_fd)
        if (not stat.S_ISREG(visible.st_mode)
                or (visible.st_dev, visible.st_ino) != identity
                or (final.st_dev, final.st_ino) != identity
                or stat.S_IMODE(final.st_mode) != 0o600):
            raise OSError("export file changed before completion")
        try:
            os.close(file_fd)
        except OSError:
            # On Linux the descriptor is no longer safe to reuse after close reports
            # an error. The directory entry is still checked below before cleanup.
            file_fd = None
            raise
        file_fd = None
        return {
            "status": "exported", "path": result_path,
            "record_id": snapshot["record_id"], "revision": snapshot["revision"],
        }
    except FileExistsError:
        return {"status": "unavailable", "reason": "output_exists", "path": result_path}
    except (OSError, ValueError):
        reason = "output_cleanup_unknown" if created_file else "output_unavailable"
        return {"status": "unavailable", "reason": reason, "path": result_path}
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
