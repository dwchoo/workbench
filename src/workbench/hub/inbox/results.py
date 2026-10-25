"""Request results (TI-14; Task Inbox SPEC "결과 조회", D-22, P2-AC-31).

- :func:`summary`: the default result: the manager's ``close`` summary, or before it the linked Task's state and
  run lines from the last ``task_view``. No backend call.
- :meth:`Results.full`: the linked Task's runs, worker reports and commands from the running backend
  (ui_v1 ``task_status{detail:true}``). With the backend off: the last ``task_view`` only.
- :meth:`Results.raw`: one page (≤ 128 KiB) of a raw log, only of a run or command of the request's linked Task
  (asked of the backend first) and only through the backend's ui_v1 ``raw_query`` (the Hub opens no log file).
- :meth:`Results.artifact`: one page of a single-link regular file under the WB project root, read by the Hub itself
  (same uid, no backend needed). The judgement (:func:`artifact_parts`) refuses ``..``, anything outside the root,
  ``.wb``/``.git`` at any depth in any case, and any symlink on the way; the open (:func:`open_artifact`) never follows
  a symlink, never opens a FIFO or device, and re-checks the inode it read.

Every read starts with ``InboxService.get`` under the caller's principal, so a request the principal may not see is
``not_found`` before anything else happens (requesters: their own requests; keys: their scope). Errors are
:class:`InboxError` with a fixed detail (never a path or backend text).
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import stat
from typing import Any, Mapping
import uuid

from .limits import InboxError

MAX_PAGE = 131072  # SPEC 입력 한도: 전체 보기 page ``length`` 1~131072 B
BACKEND_PAGE = 32 * 1024  # ui_v1 ``raw_query`` ``limit`` bound
NOT_FOUND_SOURCE = "no such run or command for this request"


def _view(request: dict) -> dict | None:
    view = request["task"]["view"]
    return view if isinstance(view, dict) else None


def summary(request: dict) -> dict:
    """``{summary, state, outcome, task:{status, runs:[{run_id, status, exit_code}]}|None}`` of an ``inbox.get`` value."""
    view = _view(request)
    task = None
    if view is not None or request["task"]["task_id"] is not None:
        view = view or {}
        runs = [{"run_id": run.get("run_id"), "status": run.get("status"), "exit_code": run.get("exit_code")}
                for run in view.get("runs") or [] if isinstance(run, dict)]
        task = {"status": view.get("status"), "runs": runs}
    text = request["result_summary"]
    if text is None and task is not None:
        lines = [f"Task {request['task']['task_id']}: {task['status']}"]
        lines += [f"run {run['run_id']}: {run['status']} (exit {run['exit_code']})" for run in task["runs"]]
        text = "\n".join(lines)
    return {"summary": text, "state": request["state"], "outcome": request["outcome"], "task": task}


class BackendResults:
    """The default port: the project's running backend over ui_v1 (``workbench.hub.inbox.backend_port``)."""

    @staticmethod
    def task_status(root: Path, task_ids: list[str], *, detail: bool = False) -> list[dict]:
        from . import backend_port
        return backend_port.task_status(root, task_ids, detail=detail)

    @staticmethod
    def raw_query(root: Path, kind: str, source_id: str, offset: int, limit: int) -> dict:
        """ui_v1 ``raw_query`` (served without attaching): the backend's own exact-id read."""
        from workbench.contracts.ui_v1 import ClientType
        from . import backend_port
        reply = backend_port.call(root, ClientType.RAW_QUERY, source_kind=kind, source_id=source_id, offset=offset,
                                  limit=limit)
        result = reply.get("result")
        return result if isinstance(result, dict) else {}


# BackendPortError reasons a client may see as is; anything else reads as an unreachable backend.
BACKEND_REASONS = frozenset({"backend_not_running", "backend_unreachable"})
BACKEND_DETAIL = {"backend_not_running": "the project's backend is not running",
                  "backend_unreachable": "the project's backend could not be reached"}


def _backend_error(exc: Exception) -> InboxError:
    reason = getattr(exc, "reason", None)
    code = reason if reason in BACKEND_REASONS else "backend_unreachable"
    return InboxError(code, BACKEND_DETAIL[code])


def page_bounds(offset: object, length: object) -> tuple[int, int]:
    """``offset`` ≥ 0 and ``length`` 1~:data:`MAX_PAGE` (ints, not bools) or ``invalid_input``."""
    if type(offset) is not int or offset < 0:
        raise InboxError("invalid_input", "offset must be a non-negative integer")
    if type(length) is not int or not 1 <= length <= MAX_PAGE:
        raise InboxError("invalid_input", f"length must be 1-{MAX_PAGE} bytes")
    return offset, length


def _canonical(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _page_data(data: bytes) -> dict:
    """UTF-8 text when the bytes are valid UTF-8, else ``data_base64`` (a page may also split a character)."""
    try:
        return {"text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"data_base64": base64.b64encode(data).decode("ascii")}


class _Malformed(ValueError):
    """A ``raw_query`` answer that does not have the contract's shape."""


def _int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _Malformed("not a non-negative integer")
    return value


def _b64(value: object) -> bytes:
    if not isinstance(value, str):
        raise _Malformed("dataBase64 is not a string")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise _Malformed("dataBase64 is not base64") from None


def _raw_piece(result: dict, start: int, want: int) -> tuple[bytes, list[dict], int, int | None]:
    """(bytes, missing ranges, end offset, observed size) of one ``raw_query`` answer (legacy file or segmented),
    clipped to ``[start, start + want)`` whatever the backend sent. A malformed answer raises :class:`_Malformed`."""
    read = result.get("read")
    if not isinstance(read, dict):
        raise _Malformed("read is not an object")
    size = result.get("observed_bytes")
    if size is None and isinstance(result.get("source_status"), dict):
        size = result["source_status"].get("observed_bytes")
    size = None if size is None else _int(size)
    stop = start + want
    if "dataBase64" in read:  # legacy file: one run of bytes from ``start``
        data = _b64(read["dataBase64"])[:want]
        return data, [], start + len(data), size
    chunks, gaps = read.get("chunks") or [], read.get("missing") or []
    if not isinstance(chunks, list) or not isinstance(gaps, list):
        raise _Malformed("chunks/missing are not lists")
    pieces = []
    for chunk in chunks:
        if not isinstance(chunk, dict) or not isinstance(chunk.get("range"), dict):
            raise _Malformed("chunk without a range")
        lo, hi, data = _int(chunk["range"].get("start")), _int(chunk["range"].get("end")), _b64(chunk.get("dataBase64"))
        if hi - lo != len(data):
            raise _Malformed("chunk range and data differ")
        pieces.append((lo, hi, data, None))
    for gap in gaps:
        if not isinstance(gap, dict):
            raise _Malformed("gap is not an object")
        lo, hi = _int(gap.get("start")), _int(gap.get("end"))
        if hi < lo:
            raise _Malformed("gap ends before it starts")
        pieces.append((lo, hi, None, str(gap.get("reason"))))
    data, missing, end = bytearray(), [], start
    for lo, hi, chunk, reason in sorted(pieces, key=lambda piece: piece[0]):
        clip_lo, clip_hi = max(lo, start), min(hi, stop)
        if clip_hi <= clip_lo:
            continue
        if chunk is not None:
            data += chunk[clip_lo - lo:clip_hi - lo]
        else:
            missing.append({"start": clip_lo, "end": clip_hi, "reason": reason})
        end = max(end, clip_hi)
    return bytes(data), missing, end, size


def _last_view(request: dict) -> dict | None:
    """The fields of the last ``task_view`` a full view shows with the backend off (nothing else it may hold)."""
    view = _view(request)
    if view is None:
        return None
    runs = [{name: run.get(name) for name in ("run_id", "status", "exit_code", "started_at", "ended_at")}
            for run in view.get("runs") or [] if isinstance(run, dict)]
    return {"status": view.get("status"), "closed": view.get("closed"), "closed_reason": view.get("closed_reason"),
            "runs": runs}


def _source(kind: str, source_id: object) -> list[dict]:
    return [{"source_kind": kind, "source_id": source_id}] if isinstance(source_id, str) else []


class Results:
    """Result reads over an :class:`~workbench.hub.inbox.service.InboxService` (blocking: call off the loop).

    ``port`` answers ``task_status(root, task_ids, detail=)`` like :class:`BackendResults` (the default)."""

    def __init__(self, inbox, *, port: Any = None, environment: Mapping[str, str] | None = None):
        self.inbox = inbox
        self.port = port if port is not None else BackendResults()
        self.environment = environment  # HOME/XDG for the protected directories (None: ``os.environ`` per read)

    summary = staticmethod(summary)
    max_page = MAX_PAGE

    # -- helpers ----------------------------------------------------------------------------
    def request(self, principal, request_id: str) -> dict:
        return self.inbox.get(principal, request_id)

    def root(self, principal, request: dict) -> Path:
        """The WB project root of a request the principal can see (the inbox's registered project)."""
        return Path(self.inbox.project_root(principal, request["project_id"]))

    def _task(self, principal, request: dict, *, detail: bool) -> dict | None:
        """The backend's ``task_status`` item of the linked Task; ``None`` when the backend does not know it."""
        task_id = request["task"]["task_id"]
        try:
            items = self.port.task_status(self.root(principal, request), [task_id], detail=detail)
        except InboxError:
            raise
        except Exception as exc:  # BackendPortError (or a broken answer): fixed text only
            raise _backend_error(exc) from None
        item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        if item.get("task_id") != task_id or item.get("exists") is not True:
            return None
        return item

    # -- full view --------------------------------------------------------------------------
    def full(self, principal, request_id: str) -> dict:
        """``summary`` plus ``full``: ``{available: true, task: {...}|None}`` or, when the backend cannot answer,
        ``{available: false, reason, view}`` (the last ``task_view``)."""
        request = self.request(principal, request_id)
        value = summary(request)
        if request["task"]["task_id"] is None:
            value["full"] = {"available": True, "task": None}
            return value
        try:
            item = self._task(principal, request, detail=True)
        except InboxError as exc:
            if exc.code not in BACKEND_REASONS:
                raise
            value["full"] = {"available": False, "reason": exc.code, "view": _last_view(request)}
            return value
        if item is None:
            value["full"] = {"available": False, "reason": "task_not_found", "view": _last_view(request)}
            return value
        value["full"] = {"available": True, "task": self._task_value(item)}
        return value

    @staticmethod
    def _task_value(item: dict) -> dict:
        runs = []
        for run in item.get("runs") or []:
            if not isinstance(run, dict):
                continue
            sources = [{"source_kind": "run", "source_id": source.get("source_id")}
                       for source in run.get("log_sources") or []
                       if isinstance(source, dict) and source.get("source_kind") == "run"
                       and isinstance(source.get("source_id"), str)]
            runs.append({"run_id": run.get("run_id"), "status": run.get("status"), "exit_code": run.get("exit_code"),
                         "started_at": run.get("started_at"), "ended_at": run.get("ended_at"),
                         "stopped_by": run.get("stopped_by"), "raw": sources})
        commands = [{"command_id": command.get("command_id"), "owner": command.get("owner"),
                     "exit_code": command.get("exit_code"), "raw": _source("command", command.get("command_id"))}
                    for command in item.get("commands") or [] if isinstance(command, dict)]
        reports = [{"message_id": report.get("message_id"), "kind": report.get("kind"), "at": report.get("at"),
                    "text": report.get("text")}
                   for report in item.get("reports") or [] if isinstance(report, dict)]
        return {"task_id": item.get("task_id"), "status": item.get("status"),
                "closed_reason": item.get("closed_reason"), "revision": item.get("revision"),
                "runs": runs, "commands": commands, "reports": reports}

    # -- raw pages --------------------------------------------------------------------------
    @staticmethod
    def _sources(item: dict) -> dict[str, str]:
        """``source_id -> source_kind`` of the Task's runs and commands (the only raw sources a request reaches)."""
        sources: dict[str, str] = {}
        for run in item.get("runs") or []:
            if not isinstance(run, dict):
                continue
            if isinstance(run.get("run_id"), str):
                sources[run["run_id"]] = "run"
            for source in run.get("log_sources") or []:
                if isinstance(source, dict) and source.get("source_kind") in ("run", "command") \
                        and isinstance(source.get("source_id"), str):
                    sources[source["source_id"]] = source["source_kind"]
        for command in item.get("commands") or []:
            if isinstance(command, dict) and isinstance(command.get("command_id"), str):
                sources[command["command_id"]] = "command"
        return sources

    def raw(self, principal, request_id: str, run_id: object, offset: object = 0, length: object = MAX_PAGE) -> dict:
        """``{run_id, source_kind, offset, length, size, next_offset, text|data_base64, missing}``: ``length`` is the
        bytes returned; ``missing`` lists absolute ranges the store no longer has (segmented logs)."""
        request = self.request(principal, request_id)
        offset, length = page_bounds(offset, length)
        if request["task"]["task_id"] is None or not _canonical(run_id):
            raise InboxError("not_found", NOT_FOUND_SOURCE)
        item = self._task(principal, request, detail=False)
        kind = None if item is None else self._sources(item).get(run_id)
        if kind is None:
            raise InboxError("not_found", NOT_FOUND_SOURCE)
        root = self.root(principal, request)
        data, missing, cursor, size, more, reached = bytearray(), [], offset, None, False, offset
        while cursor < offset + length:
            want = min(BACKEND_PAGE, offset + length - cursor)
            try:
                result = self.port.raw_query(root, kind, run_id, cursor, want)
            except Exception as exc:
                raise _backend_error(exc) from None
            if not isinstance(result, dict) or result.get("status") not in ("available", "partial"):
                raise InboxError("not_found", "the raw log of this run is not available")
            try:
                piece, gaps, end, observed = _raw_piece(result, cursor, want)
                backend_next = result.get("next_offset")
                backend_next = None if backend_next is None else _int(backend_next)
            except _Malformed:
                raise InboxError("backend_unreachable", "the project's backend gave an answer that is not readable") \
                    from None
            size = observed if observed is not None else size
            if cursor == offset and size is not None and offset > size:
                raise InboxError("invalid_input", "offset is past the end of the log")
            data += piece
            missing += gaps
            reached = max(reached, end)
            # more after ``end``: the size says so, else the backend's own next offset does
            more = end < size if size is not None else backend_next is not None and backend_next >= end
            if not more or end <= cursor:
                break
            cursor = end
        next_offset = reached if more and reached > offset else None
        return {"run_id": run_id, "source_kind": kind, "offset": offset, "length": len(data), "size": size,
                "next_offset": next_offset, **_page_data(bytes(data)), "missing": missing}

    # -- artifacts --------------------------------------------------------------------------
    def artifact(self, principal, request_id: str, path: object, offset: object = 0, length: object = MAX_PAGE) -> dict:
        """``{path, offset, length, size, next_offset, text|data_base64}`` of a regular file under the project root
        (rules: :func:`artifact_parts`, :func:`open_artifact`). No backend call: the Hub reads it as the same uid."""
        request = self.request(principal, request_id)
        offset, length = page_bounds(offset, length)
        registered = self.root(principal, request)
        protected = protected_directories(os.environ if self.environment is None else self.environment)
        try:
            root = os.path.realpath(registered)
            parts = artifact_parts(root, str(registered), path)
            refuse_protected(root, parts, protected)  # before the open: a protected directory is never "a file"
            fd = open_artifact(root, parts)
            try:
                refuse_protected(root, parts, protected, opened=fd)  # the opened file's real ancestors
                size = os.fstat(fd).st_size
                if offset > size:
                    raise InboxError("invalid_input", "offset is past the end of the file")
                data = os.pread(fd, min(length, size - offset), offset)
            finally:
                os.close(fd)
        except OSError:  # a path changing under the judgement or the open: nothing was read
            raise InboxError("not_found", NO_FILE) from None
        end = offset + len(data)
        return {"path": "/".join(parts), "offset": offset, "length": len(data), "size": size,
                "next_offset": end if end < size else None, **_page_data(data)}


# --- artifact path rules (SPEC "결과 조회" 산출물; TI-T10) ------------------------------------------------
EXCLUDED_NAMES = frozenset({".wb", ".git"})  # matched case-insensitively at any depth
MAX_PATH_BYTES = 4096
OUTSIDE = "the path is not a file this request may read"
NOT_A_FILE = "the path is not a regular file"
NO_FILE = "no such file under the project root"


def artifact_parts(root: str, registered: str, path: object) -> tuple[str, ...]:
    """The components of ``path`` relative to ``root`` (its realpath) when the judgement allows it, else an error.

    ``path`` is relative to the root or absolute (under ``root`` or the registered spelling ``registered``). It is
    refused (``forbidden``) when it has a ``..`` component, lies outside the root, names ``.wb``/``.git`` at any
    depth (any case), or when resolving it differs from its lexical form (a symlink anywhere on the way, wherever it
    points). Empty, NUL or over-long paths, and the root itself, are ``invalid_input``.
    """
    try:
        encoded = path.encode("utf-8") if isinstance(path, str) else None  # a lone surrogate cannot name a file
    except UnicodeEncodeError:
        encoded = None
    if encoded is None or not encoded or b"\x00" in encoded or len(encoded) > MAX_PATH_BYTES:
        raise InboxError("invalid_input", "artifact_path must be a path under the project root")
    if ".." in path.split(os.sep):  # never resolved: ``a/../b`` may cross a symlink at ``a``
        raise InboxError("forbidden", OUTSIDE)
    if os.path.isabs(path):
        lexical = os.path.normpath(path)
        for base in dict.fromkeys((root, os.path.normpath(registered))):
            if lexical == base or lexical.startswith(base + os.sep):
                relative = lexical[len(base):].lstrip(os.sep) or "."
                break
        else:
            raise InboxError("forbidden", OUTSIDE)
        given = path
    else:
        relative = os.path.normpath(path)
        given = os.path.join(root, path)
    parts = tuple(part for part in relative.split(os.sep) if part not in ("", "."))
    if any(part.casefold() in EXCLUDED_NAMES for part in parts):
        raise InboxError("forbidden", OUTSIDE)
    if not parts:
        raise InboxError("invalid_input", NOT_A_FILE)
    try:
        resolved = os.path.realpath(given)
    except OSError:  # the path changed while it was resolved
        raise InboxError("not_found", NO_FILE) from None
    if resolved != os.path.join(root, *parts):  # judged on the resolved path: no symlink on the way
        raise InboxError("forbidden", OUTSIDE)
    return parts


def _refused_component(dir_fd: int, name: str) -> InboxError:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return InboxError("not_found", NO_FILE)
    return InboxError("forbidden", OUTSIDE) if stat.S_ISLNK(info.st_mode) else InboxError("not_found", NO_FILE)


def open_artifact(root: str, parts: tuple[str, ...]) -> int:
    """A read-only descriptor of the regular file ``root/parts``, opened without following any symlink.

    Each directory is opened from its parent's descriptor with ``O_NOFOLLOW`` (a component swapped for a symlink
    after the judgement fails), the last component is ``lstat``-ed first and only a single-link regular file is
    opened: as an ``O_PATH`` descriptor (which never opens a FIFO or device), re-checked by ``fstat``, then reopened
    for reading through ``/proc/self/fd`` (the same inode) and checked again, including where it now lies.
    """
    flags_dir = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        dir_fd = os.open(root, flags_dir)
    except OSError:
        raise InboxError("not_found", NO_FILE) from None
    try:
        for name in parts[:-1]:
            try:
                next_fd = os.open(name, flags_dir, dir_fd=dir_fd)
            except PermissionError:
                raise InboxError("forbidden", OUTSIDE) from None
            except OSError:
                raise _refused_component(dir_fd, name) from None
            os.close(dir_fd)
            dir_fd = next_fd
        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)  # the type before any open
        except PermissionError:
            raise InboxError("forbidden", OUTSIDE) from None
        except OSError:
            raise InboxError("not_found", NO_FILE) from None
        if stat.S_ISLNK(info.st_mode):
            raise InboxError("forbidden", OUTSIDE)
        if not stat.S_ISREG(info.st_mode):
            raise InboxError("invalid_input", NOT_A_FILE)
        if info.st_nlink != 1:  # a hard link may be any file of this uid
            raise InboxError("forbidden", OUTSIDE)
        try:
            path_fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        except OSError:
            raise InboxError("not_found", NO_FILE) from None
    finally:
        os.close(dir_fd)
    try:
        if not _same_file(os.fstat(path_fd), info):
            raise InboxError("forbidden", OUTSIDE)
        try:
            fd = os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError:
            raise InboxError("forbidden", OUTSIDE) from None
        except OSError:
            raise InboxError("not_found", NO_FILE) from None
    finally:
        os.close(path_fd)
    try:
        where = os.readlink(f"/proc/self/fd/{fd}")
        if not _same_file(os.fstat(fd), info) or where != os.path.join(root, *parts):
            raise InboxError("forbidden", OUTSIDE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _same_file(now: os.stat_result, judged: os.stat_result) -> bool:
    return (stat.S_ISREG(now.st_mode) and now.st_nlink == 1
            and (now.st_dev, now.st_ino) == (judged.st_dev, judged.st_ino))


# --- protected directories (security review P2-3) ---------------------------------------------------------
def protected_directories(environment: Mapping[str, str]) -> frozenset[tuple[int, int]]:
    """``(st_dev, st_ino)`` of the Hub config and state directories, ``~/.omp`` and ``~/.ssh`` (those that exist).

    Identity, not names: a directory reached under another name or through a project root is still the same one."""
    from ..config import config_dir
    from ..paths import hub_layout
    home = environment.get("HOME") or os.path.expanduser("~")
    candidates = [config_dir(environment), Path(home) / ".config" / "omp-workbench",
                  hub_layout(environment).state_dir.parent, Path(home) / ".local" / "state" / "omp-workbench",
                  Path(home) / ".omp", Path(home) / ".ssh"]
    found = set()
    for candidate in candidates:
        try:
            info = os.stat(candidate)
        except (OSError, ValueError):
            continue
        if stat.S_ISDIR(info.st_mode):
            found.add((info.st_dev, info.st_ino))
    return frozenset(found)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def refuse_protected(root: str, parts: tuple[str, ...], protected: frozenset, *, opened: int | None = None) -> None:
    """``forbidden`` when the root, any directory above it, or any directory on ``parts`` (the leaf too, when it is
    a directory) is a protected directory. With ``opened`` (the artifact's descriptor) the walk also confirms that the
    directory entry it reaches is that same single-link file, so the checked chain is the file's real ancestry."""
    if not protected:
        return
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        current = os.open(root, flags)
    except OSError:
        if opened is not None:
            raise InboxError("forbidden", OUTSIDE) from None
        return
    try:
        # the root and its ancestors, by walking ``..`` until it stops changing (``/``)
        probe = os.dup(current)
        try:
            seen = None
            while True:
                info = os.fstat(probe)
                if _identity(info) in protected:
                    raise InboxError("forbidden", OUTSIDE)
                if _identity(info) == seen:
                    break
                seen = _identity(info)
                parent = os.open("..", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=probe)
                os.close(probe)
                probe = parent
        finally:
            os.close(probe)
        for index, name in enumerate(parts):
            try:
                info = os.stat(name, dir_fd=current, follow_symlinks=False)
            except OSError:
                if opened is not None:
                    raise InboxError("forbidden", OUTSIDE) from None
                return  # the open reports a missing path
            if stat.S_ISDIR(info.st_mode) and _identity(info) in protected:
                raise InboxError("forbidden", OUTSIDE)
            if index == len(parts) - 1:
                if opened is not None and _identity(info) != _identity(os.fstat(opened)):
                    raise InboxError("forbidden", OUTSIDE)
                return
            try:
                child = os.open(name, flags, dir_fd=current)
            except OSError:
                if opened is not None:
                    raise InboxError("forbidden", OUTSIDE) from None
                return
            os.close(current)
            current = child
            if _identity(os.fstat(current)) in protected:
                raise InboxError("forbidden", OUTSIDE)
    finally:
        os.close(current)
