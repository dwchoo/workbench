"""``registry.json``: the user's list of Workbench projects (SPEC "Registry 계약").

Any ``wb`` process may write (``ensure``/``remove`` under ``registry.lock``); the Hub only
reads. A damaged registry is never overwritten.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import time
import uuid

from workbench.backend.paths import DataDirError, write_private_json

from .paths import HubLayout, prepare_state_dir


class RegistryError(RuntimeError):
    def __init__(self, reason: str, detail: str):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}")


class RegistryMismatch(RegistryError):
    """The root is registered under another project_id."""


@dataclass(frozen=True, slots=True)
class Entry:
    root: str
    project_id: str
    name: str
    registered_at: float
    source: str

    def to_json(self) -> dict:
        return {"root": self.root, "project_id": self.project_id, "name": self.name,
                "registered_at": self.registered_at, "source": self.source}


@dataclass(frozen=True, slots=True)
class EnsureResult:
    added: bool
    entry: Entry


SOURCES = ("start", "init", "add")
MAX_BYTES = 4 << 20
ENTRY_KEYS = {"root", "project_id", "name", "registered_at", "source"}


def _invalid(layout: HubLayout, why: str) -> RegistryError:
    return RegistryError("registry_invalid", f"{layout.registry}: {why}")


def _uuid_text(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _entry(layout: HubLayout, item: object) -> Entry:
    if not isinstance(item, dict) or set(item) != ENTRY_KEYS:
        raise _invalid(layout, "project entry has unexpected keys")
    root, name = item["root"], item["name"]
    if (not isinstance(root, str) or not os.path.isabs(root) or not isinstance(name, str)
            or not _uuid_text(item["project_id"]) or item["source"] not in SOURCES
            or isinstance(item["registered_at"], bool) or not isinstance(item["registered_at"], (int, float))):
        raise _invalid(layout, f"project entry is malformed: {item!r}")
    return Entry(root, item["project_id"], name, item["registered_at"], item["source"])


def read(layout: HubLayout) -> list[Entry]:
    """Entries in registration order. A missing file is empty; any damage is ``registry_invalid``."""
    try:
        directory = layout.state_dir.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _invalid(layout, f"cannot inspect the state directory: {exc.strerror}") from exc
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid():
        raise _invalid(layout, "state directory is not a plain user-owned directory")
    try:
        descriptor = os.open(layout.registry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _invalid(layout, f"cannot open: {exc.strerror}") from exc
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_BYTES):
            raise _invalid(layout, "must be a private (0600) regular file owned by the current user")
        raw = os.read(descriptor, MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise _invalid(layout, f"not valid JSON ({exc})") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("projects"), list):
        raise _invalid(layout, "unsupported schema")
    entries = [_entry(layout, item) for item in value["projects"]]
    if (len({e.root for e in entries}) != len(entries)
            or len({e.project_id for e in entries}) != len(entries)):
        raise _invalid(layout, "duplicate root or project_id")
    return entries


LOCK_TIMEOUT = 5.0
LOCK_POLL = 0.01


@contextmanager
def _locked(layout: HubLayout, timeout: float):
    """``registry.lock`` held ``LOCK_EX`` for the read-modify-write; ``registry_busy`` after ``timeout``."""
    try:
        prepare_state_dir(layout)
    except DataDirError as exc:
        raise _invalid(layout, str(exc)) from exc
    lock = layout.state_dir / "registry.lock"
    try:
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise RegistryError("registry_busy", f"cannot open {lock}: {exc.strerror}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise RegistryError("registry_busy", f"{lock} is not a plain lock file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise RegistryError("registry_busy", f"cannot lock {lock}: {exc.strerror}") from exc
            if time.monotonic() >= deadline:
                raise RegistryError("registry_busy", f"{lock} is held by another wb process")
            time.sleep(LOCK_POLL)
        yield
    finally:
        os.close(descriptor)  # closing releases the flock


def _write(layout: HubLayout, entries: list[Entry]) -> None:
    write_private_json(layout.registry, {"schema_version": 1, "projects": [e.to_json() for e in entries]})


def ensure(layout: HubLayout, root: Path, project_id: str, source: str, *,
           lock_timeout: float = LOCK_TIMEOUT) -> EnsureResult:
    root = Path(root).resolve()
    with _locked(layout, lock_timeout):
        entries = read(layout)
        for entry in entries:
            if entry.root == str(root):
                if entry.project_id == project_id:
                    return EnsureResult(False, entry)
                raise RegistryMismatch("registry_mismatch", f"{root} is registered with project_id {entry.project_id}")
            if entry.project_id == project_id:
                raise RegistryError("registry_id_conflict", f"project_id {project_id} is registered at {entry.root}")
        entry = Entry(str(root), project_id, root.name, time.time(), source)
        _write(layout, [*entries, entry])
        return EnsureResult(True, entry)


def remove(layout: HubLayout, root: Path, *, lock_timeout: float = LOCK_TIMEOUT) -> Entry | None:
    root = Path(root).resolve()
    with _locked(layout, lock_timeout):
        entries = read(layout)
        removed = next((e for e in entries if e.root == str(root)), None)
        if removed is not None:
            _write(layout, [e for e in entries if e is not removed])
        return removed
