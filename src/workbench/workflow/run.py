"""Approved Task to worktree, persistent host shell, and worker evidence report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import time
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from workbench.contracts.ports_v2 import dispatch_allowed
from workbench.contracts.v1 import ActorRole, MessageKind
from workbench.ipc.bridge_g3.mailbox import MailboxStatus, TaskMailbox
from workbench.storage.log_raw.admission import MetadataAdmissionGate
from workbench.storage.log_raw.port import DisabledSegmentedRawPort, raw_read_command
from workbench.tasks.repository import AuthorizationError, TaskRepository
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.terminal.shell_persistent.adapter import PersistentShell
from .worktree import PreparedWorktree, WorktreePreparationError, prepare_execution_worktree
from .worker_port import WorkerResponseRejected, WorkerRolePort, verified_worker_response


class WorkflowHeld(RuntimeError):
    """No further automatic action is authorized or safely observable."""


class WorkerJudgmentUnavailable(WorkflowHeld):
    """smoke-04 G2/G3: the staged worker analysis was rejected, or its outcome is unknown.

    No worker judgment exists and the run sent no report (CW-10: nothing is replayed). ``reason`` is a short
    machine code (never response text); ``host_evidence`` is what Workbench itself checked (``judgment`` and
    ``reasons`` of the evidence guard), so the caller can tell the manager what was and was not judged.
    """

    def __init__(self, reason: str, host_evidence: Mapping[str, Any]):
        self.reason = reason
        self.host_evidence = dict(host_evidence)
        super().__init__(f"worker analysis unavailable ({reason}): no worker judgment, no report or replay")


def _start_ticks(pid: Any) -> int | None:
    if type(pid) is not int or pid < 1:
        return None
    try:
        from workbench.runtime.process_evidence import LinuxProcessProbe
        return LinuxProcessProbe.start_ticks(pid)
    except (OSError, ValueError):
        return None


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class _ExactByteMatcher:
    """Emit disjoint observed ranges, retaining only a possible cross-drain match suffix."""

    def __init__(self, pattern: bytes):
        self.pattern = pattern
        self.pending = bytearray()
        self.offset = 0
        self.found = False

    def feed(self, data: bytes) -> list[tuple[int, bytes, bool]]:
        if not data:
            return []
        if self.found:
            start = self.offset
            self.offset += len(data)
            return [(start, data, False)]
        self.pending.extend(data)
        self.offset += len(data)
        base = self.offset - len(self.pending)
        position = self.pending.find(self.pattern)
        if position >= 0:
            segments = []
            if position:
                segments.append((base, bytes(self.pending[:position]), False))
            segments.append((base + position, bytes(self.pending[position:position + len(self.pattern)]), True))
            tail = bytes(self.pending[position + len(self.pattern):])
            if tail:
                segments.append((base + position + len(self.pattern), tail, False))
            self.pending.clear()
            self.found = True
            return segments
        safe = max(0, len(self.pending) - len(self.pattern) + 1)
        if not safe:
            return []
        segment = (base, bytes(self.pending[:safe]), False)
        del self.pending[:safe]
        return [segment]

    def finish(self) -> list[tuple[int, bytes, bool]]:
        if not self.pending:
            return []
        segment = (self.offset - len(self.pending), bytes(self.pending), False)
        self.pending.clear()
        return [segment]


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _execution(spec: Mapping[str, Any]) -> dict[str, Any]:
    execution = spec.get("execution")
    if not isinstance(execution, dict) or set(execution) != {"source", "commit", "command", "criteria", "environment", "shell"}:
        raise ValueError("TaskSpec.execution must contain source, commit, command, criteria, environment, shell")
    if not isinstance(execution["source"], str) or not isinstance(execution["commit"], str):
        raise ValueError("source and commit must be strings")
    command = execution["command"]
    if not (isinstance(command, str) or isinstance(command, list) and command and all(isinstance(v, str) for v in command)):
        raise ValueError("command must be shell text or nonempty argv")
    criteria = execution["criteria"]
    if (not isinstance(criteria, dict) or set(criteria) != {"log_contains", "result_file", "result_contains"}
            or any(not isinstance(criteria[key], str) or not criteria[key] for key in criteria)):
        raise ValueError("criteria must name required log text and result-file text")
    environment = execution["environment"]
    if isinstance(environment, list):
        if (any(not isinstance(name, str) or not name for name in environment)
                or len(set(environment)) != len(environment)):
            raise ValueError("environment names must be unique nonempty strings")
    elif not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()):
        raise ValueError("environment must contain names or a legacy string mapping")
    if execution["shell"] not in {"bash", "sh"}:
        raise ValueError("only Bash/sh execution is supported")
    return execution


def _current_authority(repository: TaskRepository, task_id: str, revision: int, run_id: str,
                       approval_hash: str, automation_source: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    """Re-read public metadata and automation immediately before new work."""
    state = dict(automation_source())
    if not dispatch_allowed(state):
        raise WorkflowHeld("current automation state denies new work")
    active = repository.get_current_run(task_id)
    if active is None or active["run_id"] != run_id or active["revision"] != revision:
        raise WorkflowHeld("run is no longer the current approved run")
    decisions = repository.get_decisions(task_id, revision)
    approvals = [decision for decision in decisions if decision["kind"] == "scope_approved"]
    task = repository.get_task_spec(task_id, revision)
    if (not approvals or any(decision["kind"] == "revoked" for decision in decisions)
            or sha256(_canonical({"task": task, "approval": approvals[-1]})).hexdigest() != approval_hash):
        raise WorkflowHeld("Task revision authority changed")
    return state


def _deliver_report(mailbox: Any, message: Any, timeout: float | None,
                    on_submitted: Callable[[], None] | None) -> Any:
    """``mailbox.deliver`` with ``on_submitted`` when the mailbox supports it (``TaskMailbox`` does)."""
    options: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
    if on_submitted is not None:
        try:
            parameters = inspect.signature(mailbox.deliver).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        if any(p.name == "on_submitted" or p.kind is p.VAR_KEYWORD for p in parameters):
            options["on_submitted"] = on_submitted
    return mailbox.deliver(message, **options)


def _copy_confirmed_message(subject_log: Any, message: Any, receipt: Any, kind: str) -> None:
    """확인된 mailbox 전달만 복사한다. 미확인/지연은 원문을 추정하지 않는다."""
    if subject_log is None or receipt.status is not MailboxStatus.OMP_PROCESSED:
        return
    subject_log.record_delivery(
        {"schemaVersion": 1, "kind": "task", "id": message.task_id},
        message_id=message.message_id, revision=message.revision, kind=kind,
        content=json.dumps(dict(message.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        delivered_at=_observed_at(),
    )


def _copy_confirmed_decision(subject_log: Any, task_id: str, revision: int,
                             response: Mapping[str, Any]) -> None:
    """verified_worker_response가 돌려준 결정만 source event로 복사한다."""
    if subject_log is None:
        return
    subject_log.record_delivery(
        {"schemaVersion": 1, "kind": "task", "id": task_id},
        message_id=response["response_id"], revision=revision, kind="decision",
        content=json.dumps(dict(response), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        delivered_at=_observed_at(),
    )


def _latch_condition_metadata(gate: MetadataAdmissionGate | None,
                              outcome: Callable[[str, BaseException | None], None] | None,
                              exc: Exception) -> None:
    if outcome is not None:
        try:
            outcome("condition_evidence", exc)
            return
        except Exception:
            pass  # 제품 callback 장애도 진행 중인 collector를 중단하지 않는다.
    if gate is not None:
        gate.record_metadata_failure("condition_evidence:" + type(exc).__name__)


def classify_worker_request(request: Mapping[str, Any], approved_paths: list[str] | None) -> str:
    """Advisory classification only; never grants execution authority."""
    def valid(path: Any) -> bool:
        return (isinstance(path, str) and bool(path) and not path.startswith("/")
                and not path.startswith("./") and "\\" not in path
                and all(part not in {"", ".", ".."} for part in path.rstrip("/").split("/")))

    paths = request.get("paths")
    if not isinstance(paths, list) or not paths or any(not valid(path) for path in paths):
        raise ValueError("worker request paths must be explicit relative paths")
    if approved_paths is not None:
        if not isinstance(approved_paths, list) or any(not valid(path) for path in approved_paths):
            raise ValueError("approved paths must be explicit relative paths")
        def covered(path: str) -> bool:
            request_path = PurePosixPath(path)
            return any(request_path == PurePosixPath(approved.rstrip("/"))
                       or approved.endswith("/") and request_path.is_relative_to(PurePosixPath(approved.rstrip("/")))
                       for approved in approved_paths)
        return "existing_task_scope" if all(covered(path) for path in paths) else "scope_expansion_manager_confirmation"
    if request.get("bounded_small_spec") is True and isinstance(request.get("goal"), str) and request["goal"].strip():
        return "bounded_new_spec_requires_manager_approval"
    return "scope_expansion_manager_confirmation"


@dataclass
class WorkflowRun:
    repository: TaskRepository
    mailbox: TaskMailbox
    task_id: str
    revision: int
    run_id: str
    task_message_id: str
    worktree: PreparedWorktree
    shell: PersistentShell
    execution: dict[str, Any]
    record_dir: Path
    raw_log: Path
    result_path: Path
    result_before: str | None
    approval_hash: str
    _record: dict[str, Any]
    _observed: set[str]
    automation_source: Callable[[], Mapping[str, Any]]
    worker_port: WorkerRolePort | None
    _terminal: bool = False
    _reported: bool = False
    # False when the caller injected the shell (the product host shell): it is never closed here.
    owns_shell: bool = True
    _report_message: Any = None
    _report_requires_code_change: bool = False
    _report_judgment: str | None = None
    _run_closed: bool = False
    # CW-19 (C-D71 (5), C-AC-24): the project's RawLogStore (64 MiB per run, 512 MiB per project, cap_source,
    # missing bytes). A full or failing store never stops the run or its collection.
    raw_store: Any = None
    segmented_raw_port: DisabledSegmentedRawPort | None = None
    raw_descriptor: dict[str, Any] | None = None
    _raw_missing: int = 0
    metadata_admission: MetadataAdmissionGate | None = None
    metadata_outcome: Callable[[str, BaseException | None], None] | None = None
    subject_log: Any = None
    subject_log_source: Callable[[], Any] | None = None
    subject_capture_expected: bool = False
    subject_capture_unknown: bool = False
    subject_failure: Callable[[], None] | None = None
    _source_ref: dict[str, Any] | None = None
    _start_boundary_id: str | None = None
    _matcher: _ExactByteMatcher | None = None
    _source_ids: list[str] | None = None
    _evidence_error: str | None = None
    _matched_proof_id: str | None = None
    _observation_finished: bool = False

    @property
    def shell_request_id(self) -> str | None:
        """The id of the host shell request this run's command was sent with (None until it was recorded)."""
        value = self._record.get("shell_request_id")
        return value if isinstance(value, str) else None

    @property
    def worker_analysis_request(self) -> Any:
        """The raw stored worker analysis request (None until one was recorded; a dict when set by this class)."""
        return self._record.get("worker_analysis_request")

    def record_snapshot(self) -> dict[str, Any]:
        """A copy of the run record as last written (what ``collect`` returns)."""
        return dict(self._record)

    def _persist(self) -> None:
        _write_json(self.result_path, self._record)

    def _raw_status(self, status: Any = None, error: str | None = None, lost: int = 0) -> None:
        self._raw_missing += lost
        current = dict(self._record.get("raw_log_status") or {})
        if status is not None:
            if self.segmented_raw_port is not None:
                try:
                    source = self.segmented_raw_port.status(self.raw_descriptor)
                except Exception as exc:
                    # The append result still distinguishes stored from missing
                    # bytes if a follow-up status scan fails. Do not count an
                    # already durable append as a second storage loss.
                    current.update({"log_format": "segmented-v1", "observed_bytes": status["end"],
                                    "stored_bytes": int(current.get("stored_bytes") or 0)
                                    + status["stored_bytes"],
                                    "missing_bytes": int(current.get("missing_bytes") or 0)
                                    + status["missing_bytes"],
                                    "storage_error": status.get("storage_error") or type(exc).__name__,
                                    "source_status": "unavailable"})
                else:
                    current = {"log_format": "segmented-v1", "stored_bytes": source["retained_bytes"],
                               "observed_bytes": source["observed_bytes"],
                               "missing_bytes": source["missing_bytes"] + source["evicted_bytes"],
                               "evicted_bytes": source["evicted_bytes"],
                               "retained_ranges": source["retained_ranges"],
                               "missing_ranges": source["missing_ranges"],
                               "range_detail_truncated": source["range_detail_truncated"],
                               "storage_error": status.get("storage_error")}
                current["last_observed_at"] = time.time()
            else:
                current = {"stored_bytes": status.stored_bytes, "observed_bytes": status.observed_bytes,
                           "dropped_bytes": status.dropped_bytes, "truncated": status.truncated,
                           "missing_bytes": status.missing_bytes, "cap_source": status.cap_source,
                           "storage_error": status.storage_error}
        if error is not None:
            current["storage_error"] = error
        if self._raw_missing:
            current["missing_bytes"] = max(int(current.get("missing_bytes") or 0), self._raw_missing)
        self._record["raw_log_status"] = current

    def _read_raw(self) -> bytes:
        """The stored raw log; a store-owned log that never got a byte does not exist yet (= empty)."""
        if self.segmented_raw_port is not None:
            raise ValueError("segmented raw requires bounded source-relative reads")
        try:
            return self.raw_log.read_bytes()
        except FileNotFoundError:
            stored = (self._record.get("raw_log_status") or {}).get("stored_bytes")
            if self.raw_store is not None and not stored:
                return b""
            raise

    def _metadata_failure(self, exc: Exception) -> None:
        if self._evidence_error is not None:
            return
        self._evidence_error = type(exc).__name__
        self._record["condition_evidence_error"] = self._evidence_error
        _latch_condition_metadata(self.metadata_admission, self.metadata_outcome, exc)

    def _copy_message(self, message: Any, receipt: Any, kind: str) -> None:
        try:
            sink = self.subject_log_source() if self.subject_log_source is not None else self.subject_log
            if sink is None and self.subject_capture_expected and receipt.status is MailboxStatus.OMP_PROCESSED:
                raise RuntimeError("confirmed subject delivery has no storage sink")
            _copy_confirmed_message(sink, message, receipt, kind)
        except Exception:
            self.subject_capture_unknown = True
            if self.subject_failure is not None:
                self.subject_failure()

    def _copy_decision(self, response: Mapping[str, Any]) -> None:
        try:
            sink = self.subject_log_source() if self.subject_log_source is not None else self.subject_log
            if sink is None and self.subject_capture_expected:
                raise RuntimeError("confirmed subject decision has no storage sink")
            _copy_confirmed_decision(sink, self.task_id, self.revision, response)
        except Exception:
            self.subject_capture_unknown = True
            if self.subject_failure is not None:
                self.subject_failure()

    def _record_observed(self, segments: list[tuple[int, bytes, bool]]) -> None:
        if self._source_ref is None or self._evidence_error is not None:
            return
        for start, data, matched in segments:
            try:
                source_id = self.repository.record_log_source(
                    {"schemaVersion": 1, "kind": "task", "id": self.task_id},
                    {"schemaVersion": 1, "source": self._source_ref, "start": start,
                     "end": start + len(data), "digest": sha256(data).hexdigest()},
                )
                try:
                    sink = self.subject_log_source() if self.subject_log_source is not None else self.subject_log
                    if sink is None and self.subject_capture_expected:
                        raise RuntimeError("observed terminal range has no storage sink")
                    if sink is not None:
                        sink.record_terminal_range({"schemaVersion": 1, "kind": "task", "id": self.task_id},
                            source_id, {"schemaVersion": 1, "source": self._source_ref, "start": start,
                            "end": start + len(data), "digest": sha256(data).hexdigest()}, observed_at=_observed_at())
                except Exception:
                    self.subject_capture_unknown = True
                    if self.subject_failure is not None:
                        self.subject_failure()
                assert self._source_ids is not None
                self._source_ids.append(source_id)
                if matched:
                    self._matched_proof_id = self.repository.record_condition_proof(
                        self.task_id, self.revision, self.run_id,
                        sha256(self._matcher.pattern).hexdigest(), matched=True,
                        observation_complete=False, source_id=source_id,
                        match_start=start, match_end=start + len(data),
                        full_match_digest=sha256(data).hexdigest(), excerpt=data[:4096],
                    )
            except Exception as exc:
                self._metadata_failure(exc)
                return

    def _finish_observation(self) -> None:
        if self._observation_finished or self._matcher is None or self._source_ref is None:
            return
        self._observation_finished = True
        self._record_observed(self._matcher.finish())
        if self._evidence_error is not None:
            return
        try:
            final_id = str(uuid4())
            self.repository.record_observation_boundary({
                "schemaVersion": 1, "eventId": final_id, "source": self._source_ref,
                "kind": "final_drain", "offset": self._matcher.offset,
                "observedAt": _observed_at(),
            })
            self._record["condition_final_boundary_id"] = final_id
            if not self._matcher.found:
                coverage_id = self.repository.record_gapfree_coverage({
                    "schemaVersion": 1, "coverageId": str(uuid4()), "source": self._source_ref,
                    "startEventId": self._start_boundary_id, "finalEventId": final_id,
                    "finalOffset": self._matcher.offset, "sourceIds": self._source_ids,
                })
                proof_id = self.repository.record_condition_proof(
                    self.task_id, self.revision, self.run_id,
                    sha256(self._matcher.pattern).hexdigest(), matched=False,
                    observation_complete=True, coverage_id=coverage_id,
                )
                self._record["condition_negative_proof_id"] = proof_id
        except Exception as exc:
            self._metadata_failure(exc)

    def _drain(self) -> None:
        data = self.shell.display_bytes()
        if not data:
            return
        if self._matcher is not None:
            if self._observation_finished:
                # Bytes after a claimed final drain contradict its negative coverage.
                # Journal them to invalidate that coverage, but never mint a late match.
                start = self._matcher.offset
                self._matcher.offset += len(data)
                segments = [(start, data, False)]
            else:
                segments = self._matcher.feed(data)
            self._record_observed(segments)
        if self.segmented_raw_port is not None:
            try:
                status = self.segmented_raw_port.append(self.raw_descriptor, data)
                self._raw_status(status)
            except Exception as exc:
                self._raw_status(error=type(exc).__name__, lost=len(data))
            return
        if self.raw_store is not None:
            try:
                status = self.raw_store.append(self.run_id, data)
            except Exception as exc:  # recorded as missing bytes; the experiment and its collection go on
                self._raw_status(error=type(exc).__name__, lost=len(data))
                return
            self._raw_status(status)
            return
        try:
            with self.raw_log.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            self._raw_status(error=type(exc).__name__, lost=len(data))

    def collect(self, *, timeout: float = 5, paused: bool = False) -> dict[str, Any]:
        """Collect shell facts even while paused; never start worker analysis here."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.shell.poll(min(0.03, max(0, deadline - time.monotonic())))
            self._drain()
            life = state["lifecycle"]
            for name, observed, kind in (
                ("accepted", life["accepted"], "accepted"),
                ("started", life["experiment_started"], "started"),
            ):
                if observed and name not in self._observed:
                    self.repository.record_shell_event(self.run_id, kind, {
                        "parent_pid": state["parent_pid"], "child_pid": life["child_pid"],
                        # CW-19: the exact identity a restarted backend proves before listing it as a survivor
                        "child_start_ticks": _start_ticks(life["child_pid"]),
                        "cwd": str(self.worktree.path), "commit": self.worktree.commit,
                        "raw_log": str(self.raw_log), "result": str(self.result_path),
                    })
                    self._observed.add(name)
            if life["input_barrier"] and not life["input_returned"]:
                self.shell.release_input()
                continue
            if state["phase"] == "unknown":
                self._record.update({"shell_state": "unknown", "exit_status": life["main_exit"],
                                     "exit_confirmed": False, "unknowns": life["unknown"] or state["held_reasons"]})
                if "unknown" not in self._observed:
                    self.repository.record_shell_event(self.run_id, "failed", dict(self._record))
                    self._observed.add("unknown")
                self._persist()
                return dict(self._record)
            if life["control_returned"] and life["input_returned"] and life["lifetime"] == "ended":
                self._drain()
                self._finish_observation()
                if self.segmented_raw_port is not None:
                    try:
                        self._record["raw_log_collected"] = {
                            "log_format": "segmented-v1", "raw_log_ref": self.raw_descriptor["raw_log_ref"],
                            "source_status": self.segmented_raw_port.status(self.raw_descriptor)}
                    except Exception as exc:
                        self._record["raw_log_collected"] = {"log_format": "segmented-v1",
                                                              "error": type(exc).__name__}
                else:
                    try:
                        collected_log = self._read_raw()
                        self._record["raw_log_collected"] = {
                            "size": len(collected_log), "sha256": sha256(collected_log).hexdigest(),
                        }
                    except OSError as exc:
                        self._record["raw_log_collected"] = {
                            "path": str(self.raw_log), "error": type(exc).__name__,
                        }
                result_file = (self.worktree.path / self.execution["criteria"]["result_file"]).resolve()
                if not result_file.is_relative_to(self.worktree.path):
                    self._record["result_collected"] = {
                        "path": str(result_file), "error": "PathEscapedWorktree",
                    }
                else:
                    try:
                        collected_result = result_file.read_bytes()
                        self._record["result_collected"] = {
                            "path": str(result_file), "size": len(collected_result),
                            "sha256": sha256(collected_result).hexdigest(),
                        }
                    except OSError as exc:
                        self._record["result_collected"] = {
                            "path": str(result_file), "error": type(exc).__name__,
                        }
                self._record.update({"shell_state": "exited", "exit_status": life["main_exit"],
                                     "exit_confirmed": True, "paused_at_collection": paused})
                if "ended" not in self._observed:
                    self.repository.record_shell_event(self.run_id, "ended", dict(self._record))
                    self._observed.add("ended")
                self._terminal = True
                self._persist()
                return dict(self._record)
        self._record.update({"shell_state": "running", "exit_status": self.shell.snapshot()["lifecycle"]["main_exit"],
                             "exit_confirmed": False, "paused_at_collection": paused})
        self._persist()
        return dict(self._record)

    def judge(self, *, paused: bool = False, requires_code_change: bool = False,
              code_change_reason: str = "",
              on_report: Callable[[str, Mapping[str, Any]], None] | None = None,
              report_hold: Callable[[], str | None] | None = None) -> dict[str, Any]:
        """Judge the collected evidence and report it to the manager (once; never replayed).

        ``on_report`` (CW-18 R1) is told ``sending`` just before the report is
        delivered and ``submitted`` once the manager OMP accepted it into its
        session (mailboxes with ``on_submitted``). With ``on_report`` the run is
        closed at that acceptance, not when the manager's turn ends: the report
        reached the manager, and a later unknown receipt changes nothing.

        ``report_hold`` (CW-19, p27-cw19-fix-01) names a hold of the manager's
        automatic work (boot/metadata/model): the report is then created but not
        delivered (``deferred``: nothing submitted) and ``retry_report`` sends it
        once the hold lifts, like a manager that was busy.
        """
        if paused:
            return {"status": "deferred_paused", "run_id": self.run_id, "raw_log": str(self.raw_log)}
        if not self._terminal or self._record.get("shell_state") != "exited":
            raise WorkflowHeld("confirmed exit and input return are required before worker judgment")
        if self._reported:
            raise WorkflowHeld("worker judgment was already reported; no replay")
        if requires_code_change and not code_change_reason.strip():
            raise ValueError("code change request needs an evidence-backed reason")
        host: dict[str, Any] = {}  # the evidence guard's judgment once known
        try:
            evidence, judgment = self._analyse(requires_code_change, code_change_reason, host)
            self._record["worker_judgment"] = evidence
            self._persist()
            message = self.mailbox.create_message(
                self.task_id, self.revision, self.run_id, ActorRole.WORKER, ActorRole.MANAGER,
                MessageKind.REPORT, evidence, in_reply_to_message_id=self.task_message_id,
            )
            self._reported = True  # The delivery attempt may succeed before its caller sees a receipt.
            self._report_message = message
            self._report_requires_code_change = requires_code_change
            self._report_judgment = judgment
            self._record["report"] = {"message_id": message.message_id, "status": "delivery_unknown"}
            self._persist()
        except WorkflowHeld:
            raise
        except Exception as exc:  # fix-05 P2: any other analysis-stage failure is no worker judgment either
            self._record["worker_judgment"] = None
            if isinstance(self._record.get("report"), dict):  # created, never delivered (nor replayed)
                self._record["report"]["status"] = "not_sent"
            self._analysis_unavailable(f"analysis_error:{type(exc).__name__}",
                                       host.get("judgment"), host.get("reasons") or [])
        hold = None
        if report_hold is not None:
            try:
                hold = report_hold()
            except Exception:
                hold = "hold_unknown"
        if hold:  # nothing is submitted: the caller delivers it with retry_report after the hold
            self._record["report"]["status"] = MailboxStatus.DEFERRED.value
            self._record["report"]["held"] = str(hold)[:64]
            self._persist()
            return dict(self._record)
        if on_report is not None:
            on_report("sending", dict(self._record))
        try:
            receipt = _deliver_report(self.mailbox, message, None, self._report_submitted(on_report))
        except BaseException:
            self._persist()
            raise
        return self._finish_report(receipt)

    def _analyse(self, requires_code_change: bool, code_change_reason: str,
                 host: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The evidence guard and the staged worker analysis: (report evidence, final judgment)."""
        _current_authority(self.repository, self.task_id, self.revision, self.run_id,
                           self.approval_hash, self.automation_source)
        criteria = self.execution["criteria"]
        evidence_errors: list[dict[str, str]] = []
        if self.segmented_raw_port is not None:
            log = None  # A source can contain gaps; proof was recorded from observed bytes before storage.
        else:
            try:
                log = self._read_raw()
            except OSError as exc:
                log = None
                evidence_errors.append({"source": "raw_log", "path": str(self.raw_log),
                                        "error": type(exc).__name__})
        collected_log = self._record.get("raw_log_collected")
        if isinstance(collected_log, dict):
            if "error" in collected_log:
                evidence_errors.append({"source": "raw_log", "path": str(self.raw_log),
                                        "error": str(collected_log["error"]) + "AtCollection"})
            elif log is not None and (len(log) != collected_log["size"]
                                      or sha256(log).hexdigest() != collected_log["sha256"]):
                evidence_errors.append({"source": "raw_log", "path": str(self.raw_log),
                                        "error": "ChangedSinceCollection"})
        result_file = (self.worktree.path / criteria["result_file"]).resolve()
        if not result_file.is_relative_to(self.worktree.path):
            result_bytes = None
            evidence_errors.append({"source": "result_file", "path": str(result_file),
                                    "error": "PathEscapedWorktree"})
        else:
            try:
                result_bytes = result_file.read_bytes()
            except OSError as exc:
                result_bytes = None
                evidence_errors.append({"source": "result_file", "path": str(result_file),
                                        "error": type(exc).__name__})
        collected_result = self._record.get("result_collected")
        if isinstance(collected_result, dict):
            if "error" in collected_result:
                evidence_errors.append({"source": "result_file", "path": str(collected_result["path"]),
                                        "error": str(collected_result["error"]) + "AtCollection"})
            elif result_bytes is not None and (str(result_file) != collected_result["path"]
                                               or len(result_bytes) != collected_result["size"]
                                               or sha256(result_bytes).hexdigest() != collected_result["sha256"]):
                evidence_errors.append({"source": "result_file", "path": str(result_file),
                                        "error": "ChangedSinceCollection"})
        result_hash = sha256(result_bytes).hexdigest() if result_bytes is not None else None
        unknowns = [f"{item['source']} {item['path']}: {item['error']}" for item in evidence_errors]
        raw_status = self._record.get("raw_log_status") if isinstance(self._record.get("raw_log_status"), dict) \
            else {}
        incomplete = bool(raw_status.get("truncated") or raw_status.get("missing_bytes")
                          or raw_status.get("dropped_bytes"))
        if incomplete:  # CW-19 (C-AC-24): the judgment says what is missing; it never assumes the rest
            if self.segmented_raw_port is not None:
                unknowns.append(f"segmented raw source incomplete: retained {raw_status.get('stored_bytes')} "
                                f"of observed {raw_status.get('observed_bytes')} bytes, "
                                f"evicted {raw_status.get('evicted_bytes')}, "
                                f"missing {raw_status.get('missing_bytes')}; inspect source ranges")
            else:
                unknowns.append(f"raw log incomplete: {raw_status.get('stored_bytes')} of "
                                f"{raw_status.get('observed_bytes')} bytes stored (cap {raw_status.get('cap_source')}, "
                                f"missing {raw_status.get('missing_bytes')})")
        condition_digest = sha256(criteria["log_contains"].encode()).hexdigest()
        condition_proofs: list[dict[str, Any]] = []
        proof_error = None
        try:
            condition_proofs = [proof for proof in self.repository.get_condition_proofs(self.run_id)
                                if proof["task_id"] == self.task_id
                                and proof["revision"] == self.revision
                                and proof["condition_digest"] == condition_digest]
        except Exception as exc:
            proof_error = type(exc).__name__
            unknowns.append("condition proof read failed: " + proof_error)
        matched_proof = next((p for p in condition_proofs if p["matched"]), None)
        negative_proof = next((p for p in condition_proofs
                               if not p["matched"] and p["observation_complete"]), None)
        instrumented = self._start_boundary_id is not None
        if self._evidence_error is not None:
            unknowns.append("condition evidence write failed: " + self._evidence_error)
        for proof in condition_proofs:
            if proof.get("invalidation_reason"):
                unknowns.append("condition coverage invalidated: " + proof["invalidation_reason"])
        if self._record["exit_status"] != 0:
            judgment, reasons = "failure", ["nonzero_exit"]
        elif any(item["source"] == "raw_log" for item in evidence_errors):
            # A post-collection raw artifact changed or became unreadable. This is distinct
            # from an append-time quota loss, and the legacy tamper guard remains conservative.
            judgment, reasons = "indeterminate", ["insufficient_raw_log"]
        elif result_bytes is None or not result_bytes or any(item["source"] == "result_file" for item in evidence_errors):
            judgment, reasons = "indeterminate", ["missing_or_empty_result_file"]
            if result_bytes == b"":
                unknowns.append("result file is empty")
        elif self.result_before is not None and self.result_before == result_hash:
            judgment, reasons = "indeterminate", ["result_provenance_unconfirmed"]
            unknowns.append("result file is unchanged from before the run")
        elif criteria["result_contains"].encode() not in result_bytes:
            judgment, reasons = "failure", ["exit_zero_criteria_failed"]
        elif instrumented:
            if (proof_error is not None or (matched_proof is None and negative_proof is None)
                    or (self._evidence_error is not None and matched_proof is None)
                    or (matched_proof is not None and negative_proof is not None)):
                judgment, reasons = "indeterminate", ["condition_observation_incomplete"]
            elif negative_proof is not None and matched_proof is None:
                judgment, reasons = "failure", ["exit_zero_criteria_failed"]
            else:
                judgment, reasons = "success", ["criteria_met"]
        elif self.segmented_raw_port is not None and not instrumented:
            judgment, reasons = "indeterminate", ["condition_observation_incomplete"]
        elif (self.segmented_raw_port is None and
              (log is None or any(item["source"] == "raw_log" for item in evidence_errors) or not log)):
            judgment, reasons = "indeterminate", ["insufficient_raw_log"]
        elif incomplete and criteria["log_contains"].encode() not in log:
            judgment, reasons = "indeterminate", ["raw_log_incomplete"]
        elif criteria["log_contains"].encode() not in log:
            judgment, reasons = "failure", ["exit_zero_criteria_failed"]
        else:
            judgment, reasons = "success", ["criteria_met"]
        evidence = {
            "task_id": self.task_id, "revision": self.revision, "run_id": self.run_id,
            "judgment": judgment, "reasons": reasons, "unknowns": unknowns,
            "evidence_errors": evidence_errors,
            "exit_status": self._record["exit_status"], "exit_confirmed": True,
            "raw_log": str(self.raw_log), "raw_log_sha256": None if log is None else sha256(log).hexdigest(),
            "raw_log_excerpt": None if log is None else log[-1024:].decode("utf-8", "replace"),
            "result_file": str(result_file), "result_sha256": result_hash,
            "result_excerpt": None if result_bytes is None else result_bytes[:1024].decode("utf-8", "replace"),
            "criteria": criteria, "requires_code_change": requires_code_change,
            "code_change_reason": code_change_reason,
            "condition_evidence": {"condition_digest": condition_digest,
                                   "matched_proof_id": None if matched_proof is None else matched_proof["proof_id"],
                                   "negative_proof_id": None if negative_proof is None else negative_proof["proof_id"],
                                   "instrumented": instrumented},
        }
        if self.segmented_raw_port is not None:
            evidence.update({"log_format": "segmented-v1",
                             "raw_log_ref": self.raw_descriptor["raw_log_ref"],
                             "read_command": raw_read_command(self.raw_log.parents[3], "run", self.run_id),
                             "raw_source_status": self._record.get("raw_log_status")})
        host.update({"judgment": judgment, "reasons": list(reasons)})
        if self.worker_port is not None:
            question = self.mailbox.create_message(
                self.task_id, self.revision, self.run_id, ActorRole.MANAGER, ActorRole.WORKER,
                MessageKind.QUESTION,
                {"stage": "analysis", "revision": self.revision,
                 "facts": {key: value for key, value in evidence.items()
                           if key not in {"judgment", "reasons"}}},
            )
            self.worker_port.arm("analysis", question)
            _current_authority(self.repository, self.task_id, self.revision, self.run_id,
                               self.approval_hash, self.automation_source)
            try:
                question_receipt = self.mailbox.deliver(question)
            except Exception as exc:
                self._record["worker_analysis_request"] = {"message_id": question.message_id,
                                                           "status": "delivery_error"}
                self._analysis_unavailable(f"analysis_delivery_error:{type(exc).__name__}", judgment, reasons)
            self._copy_message(question, question_receipt, "manager_instruction")
            self._record["worker_analysis_request"] = {
                "message_id": question.message_id, "status": question_receipt.status.value,
            }
            self._persist()
            if question_receipt.status is MailboxStatus.DEFERRED:  # nothing was submitted: the caller asks again
                raise WorkflowHeld("worker analysis delivery was not confirmed; no report or replay")
            if question_receipt.status is not MailboxStatus.OMP_PROCESSED:
                self._analysis_unavailable(f"analysis_delivery_{question_receipt.status.value}", judgment, reasons)
            try:
                response = verified_worker_response(
                    "analysis", question, question_receipt,
                    self.worker_port.observe("analysis", question, question_receipt),
                )
            except WorkflowHeld:
                raise
            except WorkerResponseRejected as exc:  # the bridge's machine reason (no response text)
                self._analysis_unavailable(exc.reason, judgment, reasons)
            except TimeoutError:
                self._analysis_unavailable("worker_response_timeout", judgment, reasons)
            except ValueError:  # identity/linkage of the public response did not verify
                self._analysis_unavailable("worker_response_unverified", judgment, reasons)
            self._copy_decision(response)
            if ((judgment == "indeterminate" and response["decision"] != "indeterminate")
                    or (judgment == "failure" and response["decision"] == "success")):
                self._record["worker_analysis_response"] = response
                self._persist()
                self._analysis_unavailable("worker_judgment_conflicts_with_evidence", judgment, reasons)
            judgment = response["decision"]
            evidence["judgment"] = judgment
            evidence["worker_response"] = response
            self._record["worker_analysis_response"] = response
            _current_authority(self.repository, self.task_id, self.revision, self.run_id,
                               self.approval_hash, self.automation_source)
        return evidence, judgment

    def _analysis_unavailable(self, reason: str, judgment: str | None, reasons: list[str]) -> None:
        """smoke-04 G2/G3: record why the worker analysis is unavailable and raise; nothing is reported here.

        A pause, cancel or revoked authority keeps the plain CW-10 hold (``WorkflowHeld``) instead. A failing
        ``run.json`` write does not hide the outcome (fix-05): it is noted and the caller still closes the run.
        ``judgment`` is None when the analysis failed before the evidence guard judged (no host evidence).
        """
        _current_authority(self.repository, self.task_id, self.revision, self.run_id,
                           self.approval_hash, self.automation_source)
        host = {} if judgment is None else {"judgment": judgment, "reasons": list(reasons)}
        rejected = {"stage": "analysis", "reason": reason, "host_evidence": host or None}
        self._record["worker_analysis_rejected"] = rejected
        try:
            self._persist()
        except Exception as exc:
            rejected["persist_error"] = type(exc).__name__
        raise WorkerJudgmentUnavailable(reason, host)

    def _close_reported_run(self) -> None:
        if self._run_closed:
            return
        self._run_closed = True
        if self._report_requires_code_change:
            self.repository.fail_run(self.run_id, {"requires_code_change": True, "report": self._record["report"]})
        else:
            self.repository.complete_run(self.run_id, {"judgment": self._report_judgment,
                                                       "report": self._record["report"]})

    def _report_submitted(self, on_report: Callable[[str, Mapping[str, Any]], None] | None
                          ) -> Callable[[], None] | None:
        if on_report is None:
            return None

        def submitted() -> None:  # on this thread, inside the mailbox delivery
            self._record["report"]["status"] = MailboxStatus.API_RETURNED.value
            self._record["report"]["accepted_by_manager"] = True
            self._record["instruction_ended"] = self._report_requires_code_change
            try:  # fix-05: the report reached the manager; a failing run.json write must not contradict it
                self._persist()
            except Exception as exc:
                self._record["report"]["persist_error"] = type(exc).__name__
            self._close_reported_run()
            on_report("submitted", dict(self._record))
        return submitted

    def _finish_report(self, receipt: Any) -> dict[str, Any]:
        self._copy_message(self._report_message, receipt, "report")
        self._record["report"]["status"] = receipt.status.value
        self._record["instruction_ended"] = self._report_requires_code_change
        self._persist()
        if self._report_requires_code_change or receipt.status is MailboxStatus.OMP_PROCESSED:
            self._close_reported_run()
        return dict(self._record)

    def retry_report(self, *, timeout: float = 20,
                     on_report: Callable[[str, Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
        """Deliver the same report again only while its last receipt was ``deferred``.

        A deferred receipt means nothing was submitted to the manager OMP (the
        mailbox resends only that state); any other state is never replayed.
        """
        report = self._record.get("report") or {}
        if self._report_message is None or report.get("status") != MailboxStatus.DEFERRED.value:
            raise WorkflowHeld("only a deferred (never submitted) report is delivered again; no replay")
        self._record["report"].pop("held", None)
        if on_report is not None:
            on_report("sending", dict(self._record))
        try:
            receipt = _deliver_report(self.mailbox, self._report_message, timeout, self._report_submitted(on_report))
        except BaseException:
            self._record["report"]["status"] = "delivery_unknown"
            self._persist()
            raise
        return self._finish_report(receipt)

    def close(self) -> None:
        _release_shell(self.shell, self.owns_shell)


def _complete_line(path: Path) -> str | None:
    """The first line of ``path`` once it is complete (ends with a newline), else None (missing or partial)."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    line, newline, _ = text.partition("\n")
    return line if newline and line else None


def _release_shell(shell: Any, owned: bool) -> None:
    """Close a shell the workflow created; only detach from an injected one."""
    if owned:
        shell.close()
        return
    detach = getattr(shell, "detach", None)
    if callable(detach):
        detach()


class TaskWorkflow:
    def __init__(self, repository: TaskRepository, mailbox: TaskMailbox,
                 *, worker_port: WorkerRolePort | None = None,
                 automation_source: Callable[[], Mapping[str, Any]] | None = None,
                 raw_store: Any = None,
                 segmented_raw_port: DisabledSegmentedRawPort | None = None,
                 segmented_raw_port_factory: Callable[[], DisabledSegmentedRawPort | None] | None = None,
                 metadata_admission: MetadataAdmissionGate | None = None,
                 metadata_outcome: Callable[[str, BaseException | None], None] | None = None,
                 subject_log: Any = None,
                 subject_capture_failure: Callable[[], None] | None = None):
        self.repository = repository
        self.mailbox = mailbox
        self.raw_store = raw_store  # CW-19: the project's RawLogStore for the run's raw log (None: a plain file)
        self.segmented_raw_port = segmented_raw_port
        self.segmented_raw_port_factory = segmented_raw_port_factory
        self.metadata_admission = metadata_admission
        self.metadata_outcome = metadata_outcome
        self.subject_log = subject_log
        self._subject_capture_failure = subject_capture_failure
        self.subject_capture_unknown = False
        self.worker_port = worker_port
        self.automation_source = automation_source
        self._public_mailbox = isinstance(mailbox, TaskMailbox)

    def _copy_message(self, message: Any, receipt: Any, kind: str) -> None:
        try:
            if (self.subject_log is None and self._subject_capture_failure is not None
                    and receipt.status is MailboxStatus.OMP_PROCESSED):
                raise RuntimeError("confirmed subject delivery has no storage sink")
            _copy_confirmed_message(self.subject_log, message, receipt, kind)
        except Exception:
            self._mark_subject_unknown()

    def _copy_decision(self, task_id: str, revision: int, response: Mapping[str, Any]) -> None:
        try:
            if self.subject_log is None and self._subject_capture_failure is not None:
                raise RuntimeError("confirmed subject decision has no storage sink")
            _copy_confirmed_decision(self.subject_log, task_id, revision, response)
        except Exception:
            self._mark_subject_unknown()

    def _mark_subject_unknown(self) -> None:
        self.subject_capture_unknown = True
        if self._subject_capture_failure is not None:
            try:
                self._subject_capture_failure()
            except Exception:
                pass  # subject capture does not affect the approved run outcome

    def route_worker_request(self, request: Mapping[str, Any], *, task_id: str | None = None,
                             revision: int | None = None) -> dict[str, Any]:
        """Bind a direct request to durable Task authority; never dispatch it here."""
        if (task_id is None) != (revision is None):
            raise ValueError("Task ID and revision must be supplied together")
        task = self.repository.get_task_spec(task_id, revision) if task_id is not None else None
        approvals = ([decision for decision in self.repository.get_decisions(task_id, revision)
                      if decision["kind"] == "scope_approved"] if task is not None else [])
        approved_paths = approvals[-1]["details"].get("paths") if approvals else None
        category = classify_worker_request(request, approved_paths if task is not None else None)
        paths = list(request["paths"])
        if category == "existing_task_scope" and task is not None:
            spec = dict(task["spec"])
            if (self._public_mailbox and isinstance(spec.get("execution"), Mapping)
                    and not isinstance(spec["execution"].get("environment"), list)):
                raise WorkflowHeld("routed TaskSpec may persist environment names but not values")
            spec["worker_request"] = {"goal": spec.get("goal", "existing Task scope"),
                                      "paths": paths, "classification": category}
            routed_revision = self.repository.revise_task(task_id, spec)
            return {"classification": category, "task_id": task_id, "revision": routed_revision,
                    "prior_approval_decision_id": approvals[-1]["decision_id"],
                    "approval_decision_id": None,
                    "manager_action": "approve_scope_and_proceed", "dispatch_authorized": False}
        goal = request.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise WorkflowHeld("new or expanded scope requires a stated goal")
        proposed = request.get("spec")
        if proposed is not None and not isinstance(proposed, Mapping):
            raise ValueError("proposed TaskSpec must be a mapping")
        spec = dict(proposed) if proposed is not None else ({"goal": goal} if task is None else dict(task["spec"]))
        if (self._public_mailbox and isinstance(spec.get("execution"), Mapping)
                and not isinstance(spec["execution"].get("environment"), list)):
            raise WorkflowHeld("routed TaskSpec may persist environment names but not values")
        spec["worker_request"] = {"goal": goal, "paths": paths,
                                  "classification": category}
        if task is None:
            routed_task_id = self.repository.create_task(spec)
            routed_revision = 1
        else:
            routed_task_id = task_id
            routed_revision = self.repository.revise_task(task_id, spec)
        return {"classification": category, "task_id": routed_task_id,
                "revision": routed_revision, "approval_decision_id": None,
                "manager_action": "approve_scope_and_proceed", "dispatch_authorized": False}

    def start(self, task_id: str, revision: int, *, worktree_path: str | Path,
              artifacts_root: str | Path, automation: Mapping[str, Any],
              environment_values: Mapping[str, str] | None = None,
              shell: Any | None = None,
              before_shell_input: Callable[[], None] | None = None) -> WorkflowRun:
        """Start one approved run.

        ``before_shell_input`` (CW-18 R5) is called right before the first
        keystroke into the shell (after the worker's decision and the worktree
        preparation); it may raise ``WorkflowHeld`` so that nothing is typed.

        ``shell`` (CW-18 delta) injects an existing persistent host shell, the
        product host pane, instead of a new ``PersistentShell``. The run then
        uses that shell's own environment: ``environment_values`` must be None,
        the approved names are recorded, and the shell kind must equal the
        approved ``execution.shell``. An injected shell is never closed by the
        workflow (only ``detach()`` is called when it has one). Without
        ``shell`` the behaviour is unchanged.
        """
        if self._public_mailbox and (self.worker_port is None or self.automation_source is None):
            raise WorkflowHeld("public OMP execution requires worker-response and current-automation ports")
        if self.metadata_admission is not None and not self.metadata_admission.automatic_runs_allowed:
            raise WorkflowHeld("metadata evidence failure holds new automatic work")
        automation_source = self.automation_source or (lambda: automation)
        if not dispatch_allowed(dict(automation_source())):
            raise WorkflowHeld("automation is paused, cancelled, unhealthy, or unapproved")
        task = self.repository.get_task_spec(task_id, revision)
        execution = _execution(task["spec"])
        environment_spec = execution["environment"]
        if self._public_mailbox and not isinstance(environment_spec, list):
            raise WorkflowHeld("public OMP TaskSpec must contain environment names, never values")
        injected = shell is not None
        if injected:
            if environment_values is not None:
                raise ValueError("an injected host shell runs with its own environment; pass no values")
            if not isinstance(environment_spec, list):
                raise WorkflowHeld("an injected host shell runs only TaskSpecs with environment names")
            kind = getattr(getattr(shell, "choice", None), "kind", None)
            if kind != execution["shell"]:
                raise WorkflowHeld("the host shell kind differs from the approved execution shell")
            shell_environment = {name: "" for name in environment_spec}  # names only; values stay in the shell
        elif isinstance(environment_spec, list):
            if environment_values is None or set(environment_values) != set(environment_spec) or any(
                not isinstance(value, str) for value in environment_values.values()
            ):
                raise WorkflowHeld("transient environment values must match approved variable names")
            shell_environment = dict(environment_values)
        else:
            shell_environment = dict(environment_spec)
        approvals = [d for d in self.repository.get_decisions(task_id, revision) if d["kind"] == "scope_approved"]
        if not approvals or approvals[-1]["details"].get("execution") != execution:
            raise AuthorizationError("execution is not the persisted approved TaskSpec scope")
        approval_hash = sha256(_canonical({"task": task, "approval": approvals[-1]})).hexdigest()
        try:
            if str(UUID(task_id)) != task_id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError("mailbox Task ID must be a canonical UUID") from exc
        root = Path(artifacts_root).resolve()
        if not root.is_dir():
            raise WorkflowHeld("existing artifacts root is required")
        run_id = self.repository.start_run(task_id, revision, inputs={
            "source": execution["source"], "commit": execution["commit"],
            "worktree_path": str(Path(worktree_path).resolve()), "approval_hash": approval_hash,
        })
        record_dir = root / run_id
        try:
            segmented_raw_port = (self.segmented_raw_port_factory() if self.segmented_raw_port_factory
                                  else self.segmented_raw_port)
            record_dir.mkdir(mode=0o700)
            raw_log, result_path = record_dir / "raw.log", record_dir / "run.json"
            if segmented_raw_port is not None:
                raw_log, raw_descriptor = segmented_raw_port.prepare_task(record_dir, task_id, revision, run_id)
            elif self.raw_store is not None:  # CW-19: the store owns <raw-logs>/<run_id>.log and its quota
                raw_log = Path(self.raw_store.root_path) / f"{run_id}.log"
                self.raw_store.append(run_id, b"")
            else:
                fd = os.open(raw_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
        except Exception as exc:  # OSError, or the raw-log store closed/inconsistent (RuntimeError): no half run
            self.repository.fail_run(run_id, {"stage": "artifact_setup", "error": type(exc).__name__})
            raise WorkflowHeld("durable artifact directory could not be prepared") from exc
        record: dict[str, Any] = {
            "task_id": task_id, "revision": revision, "run_id": run_id,
            "source": execution["source"], "commit": execution["commit"],
            "cwd": str(Path(worktree_path).resolve()), "environment_names": sorted(shell_environment),
            "command": execution["command"], "raw_log": str(raw_log), "result": str(result_path),
            "shell_state": "not_sent", "exit_status": None, "exit_confirmed": False,
            "worker_judgment": None, "approval_hash": approval_hash,
        }
        if segmented_raw_port is not None:
            record.update({"log_format": "segmented-v1", "raw_log_ref": raw_descriptor["raw_log_ref"],
                           "raw_source": raw_descriptor["source"],
                           "read_command": raw_read_command(record_dir.parents[2], "run", run_id)})
        if injected:
            record["shell_source"] = "injected_host_shell"
        try:
            _write_json(result_path, record)
        except BaseException as exc:
            self.repository.fail_run(run_id, {"stage": "artifact_setup", "error": type(exc).__name__})
            raise
        host_shell, shell = shell, None
        task_message = None
        try:
            task_message = self.mailbox.create_message(
                task_id, revision, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                MessageKind.TASK, {"stage": "execute", "revision": revision,
                                   "instruction": "prepare and run the approved execution",
                                   "commit": execution["commit"]},
            )
            if self.worker_port is not None:
                self.worker_port.arm("execute", task_message)
            receipt = self.mailbox.deliver(task_message)
            self._copy_message(task_message, receipt, "manager_instruction")
            record["worker_request"] = {"message_id": task_message.message_id, "status": receipt.status.value}
            _write_json(result_path, record)
            if receipt.status is not MailboxStatus.OMP_PROCESSED:
                raise WorkflowHeld("worker instruction was not confirmed processed; no execution or replay")
            if self.worker_port is not None:
                worker_decision = verified_worker_response(
                    "execute", task_message, receipt,
                    self.worker_port.observe("execute", task_message, receipt),
                )
                record["worker_execution_decision"] = worker_decision
                self._copy_decision(task_id, revision, worker_decision)
                _write_json(result_path, record)
                if worker_decision["decision"] != "execute":
                    raise WorkflowHeld("worker OMP did not authorize execution")
            _current_authority(self.repository, task_id, revision, run_id, approval_hash, automation_source)
            worktree = prepare_execution_worktree(execution["source"], execution["commit"], worktree_path)
            record.update({"cwd": str(worktree.path), "source_status_before": worktree.source_status_before,
                           "source_status_after": worktree.source_status_after})
            _write_json(result_path, record)
            _current_authority(self.repository, task_id, revision, run_id, approval_hash, automation_source)
            if before_shell_input is not None:
                before_shell_input()  # e.g. the product host shell is checked idle and held from here
            if injected:
                shell = host_shell
            else:
                executable = "/bin/bash" if execution["shell"] == "bash" else "/bin/sh"
                shell = PersistentShell(user_environment=shell_environment,
                                        choice=ShellChoice(execution["shell"], executable))
            pwd_file = record_dir / "parent-cwd.txt"
            shell.send_user((f"cd {shlex.quote(str(worktree.path))} && pwd -P > {shlex.quote(str(pwd_file))}\n").encode())
            deadline = time.monotonic() + 3
            # p27-cd69-stuck-03: the redirection creates the file before pwd writes it; only a complete
            # (newline-terminated) line is compared, within the same deadline
            reported = _complete_line(pwd_file)
            while reported is None and time.monotonic() < deadline:
                shell.poll(0.02)
                shell.display_bytes()  # Preparation output is not experiment evidence.
                reported = _complete_line(pwd_file)
            if reported is None or Path(reported).resolve() != worktree.path:
                raise WorkflowHeld("parent shell cwd could not be verified; no experiment sent")
            shell.send_user(b"wb-handoff\n")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and shell.poll(0.02)["parent_mode"] != "control_wait":
                shell.display_bytes()
            shell.claim_manager()
            shell.display_bytes()
            state = shell.snapshot()
            control = {"portVersion": 2, "kind": "ShellControl", "payload": {
                "parentPid": state["parent_pid"], "generation": state["generation"],
                "ownerEpoch": state["owner_epoch"], "requestId": str(uuid4()),
                "approvalHash": approval_hash, "phase": "accepted",
            }}
            result_file = (worktree.path / execution["criteria"]["result_file"]).resolve()
            if not result_file.is_relative_to(worktree.path):
                raise WorkflowHeld("result-file path escapes the execution worktree")
            before = sha256(result_file.read_bytes()).hexdigest() if result_file.is_file() else None
            current_automation = _current_authority(
                self.repository, task_id, revision, run_id, approval_hash, automation_source,
            )
            if self.metadata_admission is not None and not self.metadata_admission.automatic_runs_allowed:
                raise WorkflowHeld("metadata evidence failure holds new automatic work")
            source_ref = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                          "taskId": task_id, "revision": revision, "runId": run_id}
            start_boundary_id = str(uuid4())
            try:
                self.repository.record_observation_boundary({
                    "schemaVersion": 1, "eventId": start_boundary_id, "source": source_ref,
                    "kind": "start", "offset": 0, "observedAt": _observed_at(),
                })
            except Exception as exc:
                _latch_condition_metadata(self.metadata_admission, self.metadata_outcome, exc)
                raise WorkflowHeld("observation start boundary could not be persisted") from exc
            shell.submit(control, execution["command"], current_automation)
            self.repository.record_shell_event(run_id, "sent", {
                "cwd": str(worktree.path), "commit": worktree.commit,
                "environment_names": sorted(shell_environment),
                "raw_log": str(raw_log), "result": str(result_path), "parent_pid": state["parent_pid"],
            })
            record.update({"shell_state": "sent", "parent_pid": state["parent_pid"],
                           "shell_request_id": control["payload"]["requestId"]})
            _write_json(result_path, record)
            return WorkflowRun(self.repository, self.mailbox, task_id, revision, run_id,
                               task_message.message_id, worktree, shell, execution,
                               record_dir, raw_log, result_path, before, approval_hash, record, {"sent"},
                               automation_source, self.worker_port, owns_shell=not injected,
                               raw_store=None if segmented_raw_port is not None else self.raw_store,
                               segmented_raw_port=segmented_raw_port,
                               raw_descriptor=raw_descriptor if segmented_raw_port is not None else None,
                               metadata_admission=self.metadata_admission,
                               metadata_outcome=self.metadata_outcome, subject_log=self.subject_log,
                               subject_log_source=lambda: self.subject_log,
                               subject_capture_expected=self._subject_capture_failure is not None,
                               subject_failure=self._mark_subject_unknown,
                               _source_ref=source_ref, _start_boundary_id=start_boundary_id,
                               _matcher=_ExactByteMatcher(execution["criteria"]["log_contains"].encode()),
                               _source_ids=[])
        except BaseException as exc:
            if shell is not None:
                _release_shell(shell, not injected)
            preparation_error = {"type": type(exc).__name__, "detail": str(exc)}
            if isinstance(exc, WorkerResponseRejected):  # F2: the bridge's machine reason (no response text)
                preparation_error["reason"] = exc.reason
            record.update({"shell_state": "unknown" if isinstance(exc, WorkflowHeld) else "preparation_failed",
                           "preparation_error": preparation_error})
            active = self.repository.get_current_run(task_id)
            still_authorized = active is not None and active["run_id"] == run_id
            if (still_authorized and task_message is not None
                    and dispatch_allowed(dict(automation_source()))
                    and record.get("worker_request", {}).get("status") == MailboxStatus.OMP_PROCESSED.value
                    and (not self._public_mailbox or record.get("worker_execution_decision", {}).get("decision") == "execute")):
                try:
                    report_payload: dict[str, Any] = {
                        "stage": "preparation", "judgment": "indeterminate",
                        "error": record["preparation_error"], "requires_manager_resolution": True,
                    }
                    if self.worker_port is not None:
                        question = self.mailbox.create_message(
                            task_id, revision, run_id, ActorRole.MANAGER, ActorRole.WORKER,
                            MessageKind.QUESTION,
                            {"stage": "analysis", "revision": revision,
                             "facts": {"preparation_error": record["preparation_error"]}},
                        )
                        self.worker_port.arm("analysis", question)
                        _current_authority(self.repository, task_id, revision, run_id,
                                           approval_hash, automation_source)
                        question_receipt = self.mailbox.deliver(question)
                        self._copy_message(question, question_receipt, "manager_instruction")
                        record["preparation_analysis_request"] = {
                            "message_id": question.message_id, "status": question_receipt.status.value,
                        }
                        _write_json(result_path, record)
                        if question_receipt.status is not MailboxStatus.OMP_PROCESSED:
                            raise WorkflowHeld("worker preparation analysis was not confirmed")
                        response = verified_worker_response(
                            "analysis", question, question_receipt,
                            self.worker_port.observe("analysis", question, question_receipt),
                        )
                        if response["decision"] != "indeterminate":
                            raise WorkflowHeld("worker preparation judgment conflicts with missing experiment")
                        self._copy_decision(task_id, revision, response)
                        report_payload["worker_response"] = response
                        _current_authority(self.repository, task_id, revision, run_id,
                                           approval_hash, automation_source)
                    report = self.mailbox.create_message(
                        task_id, revision, run_id, ActorRole.WORKER, ActorRole.MANAGER,
                        MessageKind.REPORT, report_payload,
                        in_reply_to_message_id=task_message.message_id,
                    )
                    record["preparation_report"] = {"message_id": report.message_id, "status": "delivery_unknown"}
                    _write_json(result_path, record)
                    preparation_receipt = self.mailbox.deliver(report)
                    self._copy_message(report, preparation_receipt, "report")
                    record["preparation_report"]["status"] = preparation_receipt.status.value
                except BaseException as report_exc:
                    failed = record.setdefault("preparation_report", {"status": "not_created"})
                    failed["error"] = type(report_exc).__name__
                    if isinstance(report_exc, WorkerResponseRejected):  # G3: the bridge's machine reason
                        failed["reason"] = report_exc.reason
            _write_json(result_path, record)
            self.repository.record_shell_event(run_id, "failed", dict(record))
            if still_authorized:
                self.repository.fail_run(run_id, {"stage": "preparation", "error": record["preparation_error"]})
            try:  # CW-18 smoke-02 E3: the caller may tell the manager once, unless this run already reported
                exc.workbench_start_failure = {
                    "run_id": run_id, "revision": revision,
                    "task_message_id": None if task_message is None else task_message.message_id,
                    "manager_report": (record.get("preparation_report") or {}).get("status"),
                }
            except AttributeError:
                pass
            raise
