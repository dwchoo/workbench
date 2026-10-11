"""Disabled raw-v2 injection and bounded source descriptors.

Construction is explicit. Normal backend startup keeps using RawLogStore.
Descriptors identify a source; they never contain captured output.
"""

from __future__ import annotations

import json
import base64
import os
from pathlib import Path
import shlex
import stat
from typing import Any
from uuid import UUID, uuid4

from workbench.contracts.logs_v1 import SourceRef, canonical_json, parse_source_ref
from workbench.storage.log_raw.reader import MAX_READ_BYTES
from workbench.storage.log_raw.segments import ReadonlySegmentedRawStore, SegmentedRawStore
from workbench.storage.log_raw.store import DEFAULT_PER_RUN_LIMIT, DEFAULT_PROJECT_LIMIT

DESCRIPTOR_FORMAT = "segmented-v1"
MAX_DESCRIPTOR_BYTES = 2048


def canonical_id(value: str) -> str:
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("raw source ID must be a canonical UUID")
    return value


def task_source(task_id: str, revision: int, run_id: str) -> SourceRef:
    return parse_source_ref({"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                             "taskId": canonical_id(task_id), "revision": revision,
                             "runId": canonical_id(run_id)})


def command_source(command_id: str, task_id: str | None = None, revision: int | None = None) -> SourceRef:
    source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
              "commandId": canonical_id(command_id)}
    if (task_id is None) != (revision is None):
        raise ValueError("Task-linked command requires Task and revision together")
    if task_id is not None:
        source.update({"taskId": canonical_id(task_id), "revision": revision})
    return parse_source_ref(source)


def raw_read_command(root: Path, kind: str, identity: str) -> str:
    """A shell-safe CLI command that selects this exact private data root."""
    if kind not in ("run", "command"):
        raise ValueError("unsupported raw source kind")
    return (f"omp-workbench logs raw --{kind} {canonical_id(identity)} "
            f"--data-dir {shlex.quote(str(root.absolute()))}")


def descriptor_path(root: Path, kind: str, identity: str) -> Path:
    canonical_id(identity)
    if kind == "run":
        return root / "workflow" / "runs" / identity / "raw-descriptor.json"
    if kind == "command":
        return root / "workflow" / "terminal" / f"{identity}.json"
    raise ValueError("unsupported raw source kind")


def _private_regular(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("raw descriptor is not a private regular file")


def _private_parent_chain(root: Path, parent: Path) -> None:
    root = root.absolute()
    parent = parent.absolute()
    if not parent.is_relative_to(root):
        raise ValueError("raw source outside data root")
    for path in (root, *(root / Path(*parent.relative_to(root).parts[:index])
                         for index in range(1, len(parent.relative_to(root).parts) + 1))):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("raw source parent is not a private directory")


def read_descriptor(path: Path, *, kind: str, identity: str) -> dict[str, Any]:
    canonical_id(identity)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _private_regular(fd)
        raw = os.read(fd, MAX_DESCRIPTOR_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("raw descriptor exceeds bound")
    data = json.loads(raw)
    if (type(data) is not dict or set(data) != {"log_format", "raw_log_ref", "source", "kind", "id"}
            or data["log_format"] != DESCRIPTOR_FORMAT or data["kind"] != kind
            or data["id"] != identity):
        raise ValueError("raw descriptor identity mismatch")
    canonical_id(data["raw_log_ref"])
    source = parse_source_ref(data["source"])
    if ((kind == "run" and source.get("runId") != identity)
            or (kind == "command" and source.get("commandId") != identity)):
        raise ValueError("raw descriptor source mismatch")
    if canonical_json(data) != raw:
        raise ValueError("raw descriptor is not canonical")
    return data


def _write_descriptor(path: Path, source: SourceRef, kind: str, identity: str) -> dict[str, Any]:
    parent = path.parent
    parent_info = parent.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o077):
        raise ValueError("raw descriptor parent must be private")
    data = {"log_format": DESCRIPTOR_FORMAT, "raw_log_ref": str(uuid4()),
            "source": parse_source_ref(source), "kind": kind, "id": canonical_id(identity)}
    raw = canonical_json(data)
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("raw descriptor exceeds bound")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        _private_regular(fd)
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return data


def read_source(store: SegmentedRawStore, source: SourceRef, offset: int = 0,
                limit: int = MAX_READ_BYTES) -> dict[str, Any]:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_READ_BYTES:
        raise ValueError("raw read offset/limit outside bound")
    status = store.source_status(source)
    high = status["observed_bytes"]
    if offset >= high:
        page_status = "unknown" if status["status"] == "unknown" else "available"
        return {"log_format": DESCRIPTOR_FORMAT, "source_status": status,
                "read": {"schemaVersion": 1, "source": parse_source_ref(source), "status": page_status,
                         "chunks": [], "available": [], "missing": []}, "next_offset": None}
    end = min(high, offset + limit)
    return {"log_format": DESCRIPTOR_FORMAT, "source_status": status,
            "read": store.read_raw(source, offset, end),
            "next_offset": end if end < high else None}


class DisabledSegmentedRawPort:
    """One optional source port shared by Task runs and terminal commands."""

    def __init__(self, store: SegmentedRawStore):
        if not isinstance(store, SegmentedRawStore):
            raise TypeError("disabled raw source requires an existing segmented store")
        self.store = store

    def prepare_task(self, record_dir: Path, task_id: str, revision: int,
                     run_id: str) -> tuple[Path, dict[str, Any]]:
        path = record_dir / "raw-descriptor.json"
        return path, _write_descriptor(path, task_source(task_id, revision, run_id), "run", run_id)

    def prepare_command(self, log_root: Path, command_id: str, *,
                        task_id: str | None = None, revision: int | None = None) -> tuple[Path, dict[str, Any]]:
        path = log_root / f"{command_id}.json"
        return path, _write_descriptor(path, command_source(command_id, task_id, revision), "command", command_id)

    def append(self, descriptor: dict[str, Any], data: bytes) -> dict[str, Any]:
        return self.store.append(descriptor["source"], data)

    def status(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        return self.store.source_status(descriptor["source"])

    def read(self, descriptor: dict[str, Any], offset: int = 0,
             limit: int = MAX_READ_BYTES) -> dict[str, Any]:
        return read_source(self.store, descriptor["source"], offset, limit)


def _private_read(path: Path, bound: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _private_regular(fd)
        data = os.read(fd, bound + 1)
        if len(data) > bound:
            raise ValueError("raw record exceeds bound")
        return data
    finally:
        os.close(fd)


def raw_query(root: Path, kind: str, identity: str, *, offset: int = 0,
              limit: int = 32 * 1024,
              segmented: DisabledSegmentedRawPort | None = None) -> dict[str, Any]:
    """Exact-ID query. No path supplied by the caller is ever opened."""
    canonical_id(identity)
    if kind not in ("run", "command"):
        raise ValueError("raw query kind must be run or command")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_READ_BYTES:
        raise ValueError("raw read offset/limit outside bound")
    descriptor = descriptor_path(root, kind, identity)
    _private_parent_chain(root, descriptor.parent)
    if kind == "run":
        record_path = root / "workflow" / "runs" / identity / "run.json"
        record = json.loads(_private_read(record_path, 1_000_000))
        if type(record) is not dict or record.get("run_id") != identity:
            raise ValueError("raw run record identity mismatch")
    else:
        record = None
    try:
        data = read_descriptor(descriptor, kind=kind, identity=identity)
    except FileNotFoundError:
        data = None
    if data is not None:
        if kind == "run" and (record.get("raw_log_ref") != data["raw_log_ref"]
                              or record.get("raw_source") != data["source"]
                              or record.get("log_format") != DESCRIPTOR_FORMAT):
            raise ValueError("raw run record and descriptor differ")
        if segmented is not None:
            result = segmented.read(data, offset, limit)
        else:
            with ReadonlySegmentedRawStore(root / "raw-v2") as store:
                result = read_source(store, data["source"], offset, limit)
        return {"status": result["read"]["status"], "kind": kind, "id": identity,
                "log_path": str(descriptor), "raw_log_ref": data["raw_log_ref"],
                "read_command": raw_read_command(root, kind, identity),
                **result}
    if kind == "run" and record.get("log_format") == DESCRIPTOR_FORMAT:
        raise ValueError("segmented raw descriptor unavailable")
    if kind == "run":
        store_path = root / "raw-logs" / f"{identity}.log"
        artifact_path = root / "workflow" / "runs" / identity / "raw.log"
        if record.get("raw_log") == str(store_path):
            path = store_path
        elif record.get("raw_log") == str(artifact_path):
            path = artifact_path
        else:
            raise ValueError("legacy run record path is not a known source")
    else:
        path = root / "workflow" / "terminal" / f"{identity}.log"
    _private_parent_chain(root, path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _private_regular(fd)
        size = os.fstat(fd).st_size
        chunk = os.pread(fd, min(limit, max(0, size - offset)), offset)
    finally:
        os.close(fd)
    return {"status": "available", "kind": kind, "id": identity, "log_format": "legacy-file",
            "log_path": str(path), "observed_bytes": size,
            "read": {"start": offset, "end": offset + len(chunk),
                     "dataBase64": base64.b64encode(chunk).decode("ascii"), "missing": []},
            "next_offset": offset + len(chunk) if offset + len(chunk) < size else None}


def raw_usage(root: Path, *, segmented: DisabledSegmentedRawPort | None = None) -> dict[str, Any]:
    """Separate legacy and disabled-v2 usage without creating or repairing either store."""
    legacy = {"project_limit_bytes": DEFAULT_PROJECT_LIMIT,
              "run_limit_bytes": DEFAULT_PER_RUN_LIMIT, "terminal_limit_bytes": 64 * 1024 * 1024,
              "run_bytes": 0, "terminal_bytes": 0, "state": "available"}
    for directory, key in ((root / "raw-logs", "run_bytes"),
                           (root / "workflow" / "terminal", "terminal_bytes")):
        try:
            _private_parent_chain(root, directory)
            names = os.listdir(directory)
        except FileNotFoundError:
            continue
        except ValueError:
            legacy["state"] = "partial"
            continue
        for name in names:
            if not name.endswith(".log"):
                continue
            try:
                canonical_id(name[:-4])
                fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    _private_regular(fd)
                    legacy[key] += os.fstat(fd).st_size
                finally:
                    os.close(fd)
            except (OSError, ValueError):
                legacy["state"] = "partial"
    try:
        if segmented is not None:
            v2 = {"state": "available", **segmented.store.storage_usage()}
        else:
            with ReadonlySegmentedRawStore(root / "raw-v2") as store:
                v2 = {"state": "available", **store.storage_usage()}
    except FileNotFoundError:
        v2 = {"state": "not_allocated"}
    except (OSError, RuntimeError, ValueError) as exc:
        v2 = {"state": "unavailable", "reason": type(exc).__name__}
    return {"legacy": legacy, "segmented_v1": v2, "activation": "disabled"}
