"""Product entrypoint: ``start``, ``attach``, ``status``, ``confirm-boot`` and ``shutdown``.

``start`` attaches to a backend already serving the data dir; otherwise it
checks start requirements (Bash, then sh; OMP; bridge extension; the user's
OMP auth store and a usable Workbench OMP home, C-D64), launches a detached
backend in its own session and attaches. ``confirm-boot`` (CW-19, C-D58)
shows the environment and execution conditions after a changed boot and
confirms it; until then Workbench starts no automatic work. Exit codes:
0 success, 1 failure, 2 start requirement missing (no backend started),
3 not running.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import shlex
import sys
import time
from typing import Mapping, NamedTuple

from workbench.backend.client import ClientError, NotRunning, UiClient, run_attach
from workbench.backend.launcher import ISOLATION_CHECK_TIMEOUT, LaunchPlan, StartRequirementError, build_plan
from workbench.backend.omp_home import OmpHomeError, auth_guidance, config_dir_value, omp_root
from workbench.backend.paths import (BackendLocked, DataDirError, DataLayout, InstanceLock,
                                     ensure_private_dir, resolve_data_dir)
from workbench.backend.summary_compat_identity import current_omp_identity, compatibility_boundary
from workbench.contracts.ui_v1 import ClientType
from workbench.ui.status_workbench.projection import _plain, summary_compatibility_text

EXIT_FAILURE, EXIT_REQUIREMENT, EXIT_NOT_RUNNING = 1, 2, 3
START_TIMEOUT = 120.0
# The backend checks both roles one after the other (each bounded).
ISOLATION_WAIT = 2 * ISOLATION_CHECK_TIMEOUT + 10.0


def _records_command(args: argparse.Namespace) -> int:
    from workbench.backend.records_cli import cmd_records
    return cmd_records(args)


def _layout(args: argparse.Namespace, *, create: bool = False) -> DataLayout:
    """Resolve project context without initializing a project during queries."""
    from workbench.backend.project import resolve_project_context
    context = resolve_project_context(Path.cwd(), os.environ, args.data_dir)
    args._project_context = context
    layout = DataLayout(context.data_root, runtime_root=context.runtime_root)
    layout.check_socket_paths()
    from workbench.backend.process_identity import inspect_backend
    identity = inspect_backend(layout)
    args._backend_identity = identity
    layout = identity.layout
    if create:
        if context.needs_initialization:
            raise DataDirError(f'initialize this project with wb before writing state: {context.project_root}')
        ensure_private_dir(layout.root)
    return layout


def _running_snapshot(layout: DataLayout) -> dict | None:
    from workbench.backend.process_identity import inspect_backend, BackendIdentityError
    identity = inspect_backend(layout)
    if identity.state != 'running':
        return None
    record = identity.record
    try:
        with UiClient(identity.layout.ui_socket, name="workbench-cli", timeout=5,
                      expected_process=record["processes"]["backend"]) as client:
            snapshot = client.snapshot()
        check_snapshot_identity(identity, snapshot, layout.root)
        return snapshot
    except (NotRunning, ClientError, OSError) as exc:
        raise BackendIdentityError('live_endpoint_unavailable', layout.root) from exc


def check_snapshot_identity(identity, snapshot: dict, root: Path) -> None:
    """A live snapshot belongs to the ``running`` identity's backend (shared with the Hub list).

    pid/start_ticks/data_dir/project_dir always; project_id/runtime_root/uid/boot_id for a
    project. Raises ``BackendIdentityError`` ``snapshot_mismatch`` or
    ``snapshot_project_runtime_mismatch``.
    """
    from workbench.backend.process_identity import BackendIdentityError
    record = identity.record
    backend = snapshot.get('backend') or {}
    expected = record['processes']['backend']
    if (backend.get('pid') != expected['pid'] or (backend.get('process') or {}).get('start_ticks') != expected['start_ticks']
            or backend.get('data_dir') != record['data_dir'] or backend.get('project_dir') != record['project_dir']):
        raise BackendIdentityError('snapshot_mismatch', root)
    if identity.project_id is not None and any(backend.get(key) != record.get(key)
            for key in ('project_id', 'runtime_root', 'uid', 'boot_id')):
        raise BackendIdentityError('snapshot_project_runtime_mismatch', root)


def _bootstrap_source() -> str:
    # Import this package from its own location without exporting PYTHONPATH
    # into the backend environment inherited by OMP and the user shell.
    package_parent = str(Path(__file__).resolve().parents[2])
    return (f"import sys; sys.path.insert(0, {package_parent!r}); "
            "from workbench.backend.cli import main; raise SystemExit(main(sys.argv[1:]))")


def spawn_backend(layout: DataLayout, plan: LaunchPlan, project_dir: str) -> int:
    """Double-fork into a new session; return the backend PID (not our child)."""
    from uuid import uuid4
    from workbench.backend.process_identity import publish_startup_admission
    startup_id = uuid4().hex
    argv = [sys.executable, "-I", "-c", _bootstrap_source(), "_backend", "--data-dir", str(layout.root),
            "--project-dir", project_dir, "--runtime-root", str(layout.runtime_root or layout.root),
            "--startup-id", startup_id, *plan.to_argv()]
    read_end, write_end = os.pipe()
    gate_read, gate_write = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    intermediate = os.fork()
    if intermediate == 0:  # pragma: no cover - child side
        try:
            os.close(read_end)
            os.close(gate_write)
            os.setsid()
            backend = os.fork()
            if backend == 0:
                os.close(write_end)
                # Parent disappearance closes the pipe; a stopped parent cannot leave
                # a gated child forever. No backend lock/process starts before admission.
                if not select.select([gate_read], [], [], 5.0)[0] or os.read(gate_read, 1) != b'1':
                    os._exit(1)
                os.close(gate_read)
                devnull = os.open(os.devnull, os.O_RDWR)
                log = os.open(layout.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                os.dup2(devnull, 0)
                os.dup2(log, 1)
                os.dup2(log, 2)
                os.closerange(3, 1 << 16)
                os.chdir(project_dir)
                os.execv(sys.executable, argv)
            os.close(gate_read)
            os.write(write_end, str(backend).encode())
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(write_end)
    os.close(gate_read)
    try:
        raw = b""
        while chunk := os.read(read_end, 64):
            raw += chunk
    finally:
        os.close(read_end)
    os.waitpid(intermediate, 0)
    try:
        if not raw.isdigit():
            raise RuntimeError("backend launch failed before exec")
        pid = int(raw)
        publish_startup_admission(layout, project_dir, pid, startup_id)
        os.write(gate_write, b'1')
        return pid
    finally:
        os.close(gate_write)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            return stream.read().rsplit(b") ", 1)[1].split()[0] not in {b"Z", b"X"}
    except (OSError, IndexError):
        return False


def _wait_started(layout: DataLayout, pid: int, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _running_snapshot(layout)
        if snapshot is not None and snapshot["phase"] != "starting":
            return snapshot
        if snapshot is None and pid > 0 and not _alive(pid) and not InstanceLock(layout.lock).held_elsewhere():
            # Our backend exited and no other backend holds the data dir.
            return None
        select.select([], [], [], 0.1)
    return None


def _wait_isolation(layout: DataLayout, snapshot: dict, timeout: float = ISOLATION_WAIT,
                    stream=None) -> dict:
    """Wait (bounded) for the start-up isolation check so the summary shows its result."""
    stream = sys.stdout if stream is None else stream  # resolved per call (redirect_stdout)
    deadline = time.monotonic() + timeout
    announced = False
    while (snapshot.get("omp_isolation") or {}).get("state") == "pending" and time.monotonic() < deadline:
        if not announced:
            print("waiting for the OMP isolation check ...", file=stream, flush=True)
            announced = True
        select.select([], [], [], 0.2)
        fresh = _running_snapshot(layout)
        if fresh is None:
            break
        snapshot = fresh
    return snapshot


def _print_summary(snapshot: dict, stream=None) -> None:
    stream = sys.stdout if stream is None else stream  # resolved per call (redirect_stdout)
    backend, panes = snapshot["backend"], snapshot["panes"]
    shell = panes.get("host_shell", {}).get("shell", {})
    print(f"backend pid {backend['pid']} phase {snapshot['phase']}"
          + (f" ({snapshot['reason']})" if snapshot.get("reason") else ""), file=stream)
    print(f"project {backend.get('project_dir')} runtime {backend.get('runtime_root')}", file=stream)
    print(f"data dir {backend['data_dir']}", file=stream)
    for name, pane in panes.items():
        process = pane.get("process") or {}
        print(f"  {name}: pid {process.get('pid')} alive={pane.get('alive')} owner={pane.get('input_owner')}",
              file=stream)
    if shell:
        print(f"  shell: {shell.get('kind')} {shell.get('executable')} mode={shell.get('parent_mode')}",
              file=stream)
    _print_important_warning(snapshot, stream)
    usage = snapshot.get("usage")
    if isinstance(usage, dict) and usage.get("task_id"):  # C-AC-21 (CW-16 D-B2-3)
        model = usage.get("model") or {}
        tokens = next((f"{value} tok" + (" (추정)" if key == "tokens_estimated" else "")
                       for key in ("tokens_observed", "tokens_estimated")
                       if type(value := model.get(key)) is int), "미확인")
        print(f"사용량: Task {usage.get('task_id')} run {usage.get('runs_started')} "
              f"(재시도 {usage.get('retries_used')}/{usage.get('retry_limit')}) · 60s 점검 "
              f"{usage.get('review_count') if usage.get('review_count') is not None else '-'} · 모델 {tokens}",
              file=stream)
    isolation = snapshot.get("omp_isolation")
    if isolation:
        drift = isolation.get("version_drift") or {}
        drift_text = (" evidence " + ", ".join(f"{name}={version}" for name, version in sorted(drift.items()))
                      if drift else "")
        print(f"omp isolation: {isolation.get('state')} ({backend.get('omp_version')}{drift_text})", file=stream)
        if isolation.get("state") == "pending":
            print("  note: the isolation check has no result yet; run 'status' to see it", file=stream)
        if isolation.get("warning"):
            print(f"WARNING: {isolation['warning']}", file=stream)
        for note in isolation.get("notes") or ():
            print(f"  note: {note}", file=stream)


def _print_important_warning(snapshot: dict, stream=None) -> None:
    stream = sys.stdout if stream is None else stream  # resolved per call (redirect_stdout)
    important = snapshot.get("important_logs")
    if not isinstance(important, dict):
        return
    count = important.get("databaseCount")
    if important.get("countState") == "known" and type(count) is int and count >= 2:
        print(f"경고: 중요 로그 DB {count}개 · 총 물리 크기 {important.get('totalPhysicalBytes')} bytes. "
              "'logs important status'로 확인하고 'logs important delete-db --db UUID'로 정리하세요", file=stream)
    elif important.get("countState") != "known":
        print("경고: 중요 로그 DB 수·용량 확인 불가. 'logs important status'로 확인하세요", file=stream)

    print(summary_compatibility_text(important.get("compatibility")), file=stream)


def _print_recovery(snapshot: dict, stream=None) -> None:
    """CW-19: boot confirmation, start-up reconcile, survivors, holds and faults (user-facing, Korean)."""
    stream = stream or sys.stdout
    boot = snapshot.get("boot") or {}
    if boot.get("confirmation_required") and boot.get("reason") == "reconcile_failed":
        print("재시작 대조 실패: 이전 backend의 process·Task·메시지 상태를 확인하지 못했습니다. "
              "'python -m workbench confirm-boot' 로 환경을 확인하기 전까지 Workbench 자동 동작은 보류됩니다",
              file=stream)
    elif boot.get("confirmation_required"):
        print(f"부팅 확인 대기 ({boot.get('reason') or 'reboot'}): 'python -m workbench confirm-boot' 로 환경을 확인하기 "
              "전까지 Workbench 자동 동작은 보류됩니다", file=stream)
    startup = snapshot.get("startup") or {}
    classification = startup.get("classification")
    if classification and classification != "fresh":
        run = startup.get("run") or {}
        line = f"backend 재시작 대조: {classification}"
        if run.get("run_id"):
            line += f" | 이전 run {run.get('run_id')} 상태 {run.get('state')} (재실행·재전송 없음)"
        if startup.get("outbox_lost_count"):
            line += f" | 보내지 못한 메시지 {startup['outbox_lost_count']}건 (재전송 없음)"
        print(line, file=stream)
    for item in startup.get("survivors") or ():
        if item.get("state") not in ("alive", "stop_unconfirmed"):
            continue
        how = "manager가 stop_survivor로 종료 가능" if item.get("stoppable") else f"표시만 ({item.get('why_not')})"
        print(f"  이전 backend가 남긴 process {item.get('survivor_id')}: {item.get('name')} pid {item.get('pid')} "
              f"{item.get('comm') or ''} — {how}", file=stream)
    holds = [item.get("reason") for item in snapshot.get("holds") or ()]
    if holds:
        print(f"자동 동작 보류: {', '.join(str(reason) for reason in holds)}", file=stream)
    faults = snapshot.get("faults") or {}
    metadata = faults.get("metadata")
    if metadata:
        print(f"metadata 저장 장애: {', '.join(metadata.get('sources', {}).values())} — 실행·관측은 계속, 새 자동 작업 보류",
              file=stream)
    for role, model in sorted((faults.get("model") or {}).items()):
        if isinstance(model, dict) and model.get("state") == "error":
            print(f"모델 오류({role}): 자동 작업 보류, 실험·관측 계속 (다음 정상 응답에서 해제)", file=stream)
    raw = faults.get("raw_log")
    if isinstance(raw, dict) and raw.get("text"):
        print(f"raw log: {raw['text']}", file=stream)


def _verified_identity(layout: DataLayout):
    from workbench.backend.process_identity import inspect_backend
    identity = inspect_backend(layout)
    if identity.state != 'running':
        raise NotRunning(f'no verified backend is running for {layout.root}')
    return identity


def _verified_client(layout: DataLayout, **kwargs) -> UiClient:
    identity = _verified_identity(layout)
    return UiClient(identity.layout.ui_socket, expected_process=identity.record['processes']['backend'], **kwargs)


def _attach(layout: DataLayout, args: argparse.Namespace) -> int:
    identity = _verified_identity(layout)
    expected = identity.record['processes']['backend']
    if getattr(args, "plain", False):
        return run_attach(identity.layout.ui_socket, expected_process=expected)
    from workbench.ui.product import run_product
    return run_product(identity.layout.ui_socket, expected_process=expected)


def omp_home_location(root: Path, environment: Mapping[str, str]) -> None:
    """C-D64: the data dir must not overlap the user's OMP dirs (pure path check, creates nothing)."""
    try:
        config_dir_value(omp_root(root), environment)
    except OmpHomeError as exc:
        raise StartRequirementError(f"{exc}\nNo backend was started.") from exc


def omp_home_requirements(layout: DataLayout, environment: Mapping[str, str], *, omp: str | None = None) -> None:
    """C-D64: refuse to start without the user's OMP auth store (never created by Workbench).

    Also refused when the store's location cannot be resolved unambiguously
    from the user's own OMP variables; a provider API key variable admits a
    start without any store (the OMPs then run without the shared login).
    """
    try:
        guidance = auth_guidance(environment)
    except OmpHomeError as exc:
        raise StartRequirementError(f"{exc}\nNo backend was started.") from exc
    if guidance:
        if omp is not None:
            guidance += f'\nSelected OMP executable: {shlex.quote(omp)}. Run it outside Workbench, use /login, then run wb.'
        raise StartRequirementError(guidance + "\nNo backend was started.")
    omp_home_location(layout.root, environment)


def cmd_start(args: argparse.Namespace) -> int:
    from workbench.backend.maintenance import fence
    no_attach = args.no_attach
    args.no_attach = True
    try:
        with fence(shared=True):
            status = _cmd_start(args)
    finally:
        args.no_attach = no_attach
    if status == 0 and not no_attach:
        return _attach(_layout(args), args)
    return status


def _cmd_start(args: argparse.Namespace) -> int:
    from workbench.backend.project import initialize_project, project_initialization_lock
    from workbench.backend.process_identity import inspect_backend
    layout = _layout(args)
    context = args._project_context
    running = _running_snapshot(layout) if layout.root.exists() else None
    if running is None and InstanceLock(layout.lock).held_elsewhere():
        # Another admitted start already owns the data-dir lock. Never launch a duplicate.
        running = _wait_started(layout, 0, args.timeout)
        if running is None:
            print(f'backend did not become available: identity/startup unavailable for {layout.root}; inspect its owner before retrying', file=sys.stderr)
            return EXIT_FAILURE
    if running is None:
        try:
            omp_home_location(layout.root, os.environ)
            plan = build_plan(os.environ, omp=args.omp, omp_args=args.omp_arg or (),
                              bridge_extension=args.bridge_extension)
            omp_home_requirements(layout, os.environ, omp=plan.omp)
        except StartRequirementError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_REQUIREMENT
        if context.needs_initialization:
            try:
                context = initialize_project(context, environment=os.environ,
                    interactive=bool(sys.stdin is not None and sys.stdin.isatty()),
                    confirm=lambda root: input(f"WB project {root}. Create {root / '.wb'}? [y/N] ").strip().lower() == 'y')
            except (DataDirError, EOFError) as exc:
                print(f'project confirmation required: {context.project_root}: {exc}', file=sys.stderr)
                return EXIT_REQUIREMENT
            layout = DataLayout(context.data_root, runtime_root=context.runtime_root)
        project_root = (args._backend_identity.project_root or context.project_root)
        try:
            with project_initialization_lock(project_root, os.environ):
                identity = inspect_backend(layout)
                if identity.state == 'running':
                    layout = identity.layout
                    running = _running_snapshot(layout)
                elif identity.state == 'starting':
                    running = _wait_started(layout, 0, args.timeout)
                else:
                    ensure_private_dir(layout.root)
                    print(f'starting backend: shell {plan.shell.kind} ({plan.shell.executable}), {plan.omp_version}')
                    pid = spawn_backend(layout, plan, str(project_root))
                    running = _wait_started(layout, pid, args.timeout)
        except DataDirError as exc:
            if 'initialization/migration is locked' not in str(exc):
                raise
            # An independently admitted frontend may be between fork and lock acquisition.
            running = _wait_started(layout, 0, args.timeout)
        if running is None:
            print(f'backend did not become available; see {layout.log}', file=sys.stderr)
            return EXIT_FAILURE
    else:
        print(f"backend already running (pid {running['backend']['pid']}); attaching instead of starting")
    running = _wait_isolation(layout, running)
    _print_summary(running)
    _print_update_resume(layout)
    _register_started_project(context)
    if args.no_attach:
        return 0
    return _attach(layout, args)


def _register_started_project(context) -> None:
    """HB-04: a successful start (or join) registers the project; failure only warns."""
    if context.project_id is None or context.data_root != context.project_root / '.wb':
        return
    from workbench.hub.cli import register_quietly
    register_quietly(context.project_root, context.project_id, 'start')


def _print_update_resume(layout: DataLayout) -> None:
    from workbench.backend.paths import read_private_json
    marker = layout.root/'update-resume.json'
    if not os.path.lexists(marker):
        return
    try:
        value = read_private_json(marker)
        if not value or value.get('schema_version') != 1:
            raise ValueError('unknown maintenance marker')
        print('업데이트 재개 안내: 중단된 명령·실험을 자동 재실행하지 않습니다.')
        print('백업: '+_plain(value.get('backup')))
        for key, label in (('task','이전 작업'),('sessions','양 역할 session'),
                           ('state','마지막 확인 상태'),('shutdown','종료 결과')):
            print(label+': '+json.dumps(value.get(key),ensure_ascii=False,sort_keys=True))
        print('원래 프로젝트·기록·session을 검토한 뒤 명시적으로 재개하세요: '+_plain(value.get('resume')))
    except (OSError, ValueError):
        print('업데이트 재개 metadata를 확인할 수 없습니다. private updates manifest와 원본 기록을 검토하세요.', file=sys.stderr)


def cmd_attach(args: argparse.Namespace) -> int:
    layout = _layout(args)
    snapshot = _running_snapshot(layout)
    if snapshot is not None:
        _print_important_warning(snapshot)
    try:
        return _attach(layout, args)
    except NotRunning:
        print(f"no backend is running for {layout.root}; use 'start'", file=sys.stderr)
        return EXIT_NOT_RUNNING


def cmd_status(args: argparse.Namespace) -> int:
    layout = _layout(args)
    snapshot = _running_snapshot(layout)
    if snapshot is None:
        if args.json:
            print(json.dumps({"running": False, "data_dir": str(layout.root)}))
        else:
            print(f"no backend is running for {layout.root}")
        return EXIT_NOT_RUNNING
    if args.json:
        print(json.dumps({"running": True, "snapshot": snapshot}, sort_keys=True))
    else:
        _print_summary(snapshot)
        _print_recovery(snapshot)
    return 0


def _important_subject(args: argparse.Namespace) -> dict[str, object]:
    return {"schemaVersion": 1, "kind": "task" if args.task else "command",
            "id": args.task or args.command_id}


def _important_confirmation(args: argparse.Namespace, description: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print(f"{description}: 명시 확인이 필요합니다 (--yes)", file=sys.stderr)
        return False
    return input(f"{description}. 계속할까요? [y/N] ").strip().lower() == "y"


def cmd_summary_compat(args: argparse.Namespace) -> int:
    from workbench.backend.summary_compatibility import SummaryCompatibility
    from workbench.backend.summary_compat_identity import current_omp_identity, compatibility_boundary
    layout = _layout(args, create=args.compat_action == "verify")
    service = SummaryCompatibility(layout.summary_compatibility,
        omp_identity=current_omp_identity, boundary=compatibility_boundary)
    try:
        result = service.verify_scripted() if args.compat_action == "verify" else service.status()
    except BackendLocked:
        result = {"result": "unavailable", "reason": "compatibility_record_busy"}
    except (OSError, ValueError, KeyError, TypeError):
        result = {"result": "unavailable", "reason": "compatibility_record_invalid"}
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        if args.compat_action == "status":
            print(summary_compatibility_text(service.observed_status()))
        else:
            print("중요 로그 요약 명시 검증 결과: " + _plain(result.get("result")))
        if result.get("unknowns"):
            print("미확인: " + _plain(", ".join(result["unknowns"])))
        if result.get("reason"):
            print("사유: " + _plain(result["reason"]))
    return EXIT_FAILURE if args.compat_action == "verify" and result.get("result") != "pass" else 0


def _important_path_state(path: Path) -> str:
    """Observe only whether the dedicated important-log path is present."""
    try:
        path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    return "present"


def _important_unavailable_print(result: dict) -> None:
    unavailable = result.get("unavailable")
    print("부분 결과/조회 실패:", file=sys.stderr)
    if isinstance(unavailable, list) and unavailable:
        for item in unavailable:
            if not isinstance(item, dict):
                continue
            identity = item.get("dbUuid")
            reason = item.get("reason")
            print(f"  DB {_plain(identity) if identity else '미확인'} · "
                  f"사유 {_plain(reason) if reason else '미확인'}", file=sys.stderr)
        return
    print("  DB 미확인 · 사유 미확인", file=sys.stderr)


def _important_body_section(body: dict, key: str, label: str) -> None:
    values = body.get(key)
    if not isinstance(values, list) or not values:
        return
    print(f"{label}:")
    for value in values:
        lines = (value if isinstance(value, str) else _plain(value)).split("\n")
        print("  - " + _plain(lines[0]))
        for line in lines[1:]:
            print("    " + _plain(line))


def _important_source_print(span: dict, *, gap: bool = False) -> None:
    source = span.get("source")
    if not isinstance(source, dict):
        source = {}
    kind = _plain(source.get("sourceKind")) or "미확인"
    print(f"  - {kind}" + (f" · 사유 {_plain(span.get('reason'))}" if gap else ""))
    labels = (("taskId", "Task ID"), ("revision", "revision"), ("runId", "run ID"),
              ("commandId", "command ID"), ("messageId", "message ID"),
              ("eventId", "event ID"))
    details = [f"{label}={_plain(source[key])}" for key, label in labels if key in source]
    if details:
        print("    " + " · ".join(details))
    start, end = span.get("start"), span.get("end")
    if type(start) is int and type(end) is int:
        print(f"    범위 [{start}, {end})")


def _important_show_print(result: dict) -> None:
    for row in result.get("entries", []):
        if not isinstance(row, dict):
            continue
        entry = row.get("entry")
        if not isinstance(entry, dict):
            continue
        subject = entry.get("subject")
        if not isinstance(subject, dict):
            subject = result.get("subject")
        if not isinstance(subject, dict):
            subject = {}
        subject_kind = subject.get("kind")
        subject_label = "Task" if subject_kind == "task" else "command"
        subject_id = _plain(subject.get("id")) or "ID 미확인"
        kind = _plain(entry.get("kind")) or "종류 미확인"
        recorded = _plain(entry.get("recordedAt")) or "시각 미확인"
        print(f"{subject_label} {subject_id} · {kind} · {recorded}")
        entry_id = _plain(entry.get("entryId")) or "미확인"
        print(f"기록 ID: {entry_id}")
        body = entry.get("body")
        if isinstance(body, dict):
            for key, label in (("facts", "사실"), ("interpretations", "해석"),
                               ("unknowns", "미확인")):
                _important_body_section(body, key, label)
        sources = entry.get("sources")
        if isinstance(sources, list) and sources:
            print("출처:")
            for span in sources:
                if isinstance(span, dict):
                    _important_source_print(span)
        else:
            print("확인된 출처: 없음")
        gaps = entry.get("sourceGaps")
        if isinstance(gaps, list) and gaps:
            print("누락 범위:")
            for gap in gaps:
                if isinstance(gap, dict):
                    _important_source_print(gap, gap=True)
        databases = row.get("sourceDbUuids")
        if isinstance(databases, list) and databases:
            print("DB UUID: " + ", ".join(_plain(value) for value in databases))
        else:
            print("DB UUID: 미확인")


def _important_print(result: dict, args: argparse.Namespace, *,
                     path_state: str | None = None) -> None:
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return
    if args.logs_action == "status":
        from workbench.storage.important_log.maintenance import RETENTION_DAYS, TARGET_BYTES

        count, size = result.get("databaseCount"), result.get("physicalBytes")
        complete = result.get("complete") is True
        if complete and type(count) is int and type(size) is int:
            print(f"important DB {count}개 · 총 물리 크기 {_status_bytes(size)} · active {result.get('active')}")
            if count >= 2:
                print("경고: 여러 중요 로그 DB가 남아 있습니다. 'logs important status --json'으로 UUID와 크기를 "
                      "확인하고 'logs important delete-db --db UUID'로 사용자 정리를 진행하세요")
        else:
            print("important DB 상태: 일부 또는 확인 불가")
            count_text = f"{count}개" if type(count) is int and count >= 0 else "미확인"
            print(f"DB 수: {count_text} · 물리 크기: {_status_bytes(size)}")
            if path_state == "missing":
                print("중요 로그 저장소 경로가 없습니다.")
                print("처음 사용하는 환경이라면 첫 작업 기록이 저장된 뒤 조회할 수 있습니다.")
            elif path_state == "present":
                print("저장소 경로가 있지만 DB 정보를 조회하지 못했습니다.")
            elif path_state == "unknown":
                print("저장소 경로 존재 여부를 확인하지 못했습니다.")
        databases = result.get("databases")
        if isinstance(databases, list):
            for row in databases:
                if not isinstance(row, dict):
                    continue
                identity = row.get("dbUuid") or "UUID 미확인"
                role = row.get("role") or "unknown"
                availability = row.get("availability") or "unknown"
                print(f"  DB {identity} · role={role} · 물리 크기 {_status_bytes(row.get('physicalBytes'))}"
                      f" · 상태={availability}")
                if role == "orphan":
                    print("    orphan은 확인 불가 상태이며 archive로 추정하거나 삭제 대상으로 안내하지 않습니다")
        print(f"활성 DB 보관 안내: {_status_bytes(TARGET_BYTES)} 논리 용량 목표이며 전체 물리 용량 상한이 아닙니다")
        print(f"보관 기간: {RETENTION_DAYS}일은 보관 종료 시점부터 계산하며 용량 압박 시 더 일찍 정리될 수 있습니다")
        print("archive DB는 자동 삭제되지 않습니다. 수동 정리는 사용자가 UUID를 선택해 명시적으로 진행합니다")
        print(summary_compatibility_text(result.get("compatibility")))
    elif args.logs_action == "list":
        for row in result.get("subjects", []):
            item = row["subject"]
            print(f"{item['kind']} {item['id']} · {row['entryCount']} entries · DB "
                  f"{','.join(row['sourceDbUuids'])}")
        if not result.get("subjects") and result.get("complete"):
            print("기록 없음")
    elif args.logs_action == "show":
        _important_show_print(result)
        if not result.get("entries") and result.get("complete"):
            print("기록 없음")
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("nextAfter"):
        print(f"다음 page: {result['nextAfter']}")
    if not result.get("complete", True):
        _important_unavailable_print(result)


def cmd_important_read(args: argparse.Namespace) -> int:
    from workbench.storage.important_log import ImportantLogReader
    try:
        layout = _layout(args, create=False)
        reader = ImportantLogReader(layout.important_logs)
        if args.logs_action == "status":
            result = reader.status()
            # Reuse the running backend's observation, never perform a live compatibility check.
            snapshot = _running_snapshot(layout) or {}
            important = snapshot.get("important_logs")
            result["compatibility"] = (important.get("compatibility")
                                       if isinstance(important, dict) else None)
        elif args.logs_action == "list":
            result = reader.list_subjects(limit=args.limit, after=args.page)
        else:
            result = reader.show(_important_subject(args), limit=args.limit, after=args.page)
        path_state = None
        if (args.logs_action == "status" and not args.json
                and result.get("complete") is not True):
            path_state = _important_path_state(layout.important_logs)
        _important_print(result, args, path_state=path_state)
        return 0 if result.get("complete") else EXIT_FAILURE
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"important 조회 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


def cmd_raw_rotation(args: argparse.Namespace) -> int:
    from workbench.backend.raw_rotation import RawRotationPolicy
    from workbench.backend.summary_compatibility import SummaryCompatibility, checkout_root
    from workbench.backend.summary_compat_identity import observe_configured
    # Even --yes does not bypass the local product evidence and live gate.
    print("경고: 이후 시작한 raw source는 100MB × 5 rotation으로 오래된 bytes가 삭제될 수 있습니다. "
          "기존 source와 legacy 파일은 전환하거나 삭제하지 않습니다.", file=sys.stderr)
    if not args.yes:
        print("사용자의 명시 활성화 요청이 필요합니다 (--yes)", file=sys.stderr)
        return EXIT_FAILURE
    try:
        layout = _layout(args, create=False)
        compatibility = SummaryCompatibility(layout.summary_compatibility,
            omp_identity=current_omp_identity, boundary=compatibility_boundary)
        if not compatibility.activation_preflight(args.evidence):
            raise ValueError("verified_activation_evidence_unavailable")
        observation = observe_configured(checkout_root(), environment=os.environ)
        if observation.get("result") != "pass" or observation.get("cleanup_complete") is not True:
            raise ValueError("verified_activation_evidence_unavailable")
        fresh = observation["omp"]
        def activation_identity():
            current = current_omp_identity()
            if all(current.get(key) == fresh.get(key) for key in ("binary_path", "binary_sha256", "asset_selection")):
                return dict(fresh)
            return current
        compatibility.omp_identity = activation_identity
        policy = RawRotationPolicy(layout.raw_rotation, compatibility)
        result = policy.activate(args.evidence)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print("raw rotation 활성화 거부: " + _plain(str(exc)), file=sys.stderr)
        return EXIT_FAILURE


def _status_bytes(value: object) -> str:
    if type(value) is int and value >= 0:
        return f"{value:,} B"
    return "미확인"


def _raw_status_print(result: dict) -> None:
    from workbench.storage.log_raw.segments import SEGMENT_COUNT, SEGMENT_LIMIT

    legacy = result.get("legacy")
    legacy = legacy if isinstance(legacy, dict) else {}
    segmented = result.get("segmented_v1")
    segmented = segmented if isinstance(segmented, dict) else {}
    rotation = result.get("rotation")
    rotation = rotation if isinstance(rotation, dict) else {}

    activation = rotation.get("activated")
    if type(activation) is not bool:
        activation_value = result.get("activation")
        activation = True if activation_value == "enabled" else False if activation_value == "disabled" else None
    activation_text = "활성화됨" if activation is True else "비활성" if activation is False else "미확인"
    policy = rotation.get("newSourcePolicy")
    print("raw 원본 용량 상태")
    response_status = result.get("status")
    if isinstance(response_status, str):
        print(f"응답 상태: {_plain(response_status)}")
    response_reason = result.get("reason")
    if isinstance(response_reason, str) and response_reason:
        print(f"조회 사유: {_plain(response_reason)}")
    print(f"활성화 결정: {activation_text}")
    print(f"실제 신규 source 정책: {_plain(policy) if isinstance(policy, str) else '미확인'}")
    reason = rotation.get("reason")
    if isinstance(reason, str) and reason:
        print(f"정책 사유: {_plain(reason)}")

    legacy_state = legacy.get("state")
    legacy_label = legacy_state if isinstance(legacy_state, str) else "unknown"
    print(f"legacy 조회 상태: {legacy_label}")
    run_bytes = _status_bytes(legacy.get("run_bytes"))
    terminal_bytes = _status_bytes(legacy.get("terminal_bytes"))
    run_observed = "확인된 " if legacy_state != "available" and run_bytes != "미확인" else ""
    terminal_observed = "확인된 " if legacy_state != "available" and terminal_bytes != "미확인" else ""
    print(f"legacy 실험: {run_observed}{run_bytes} · "
          f"project 한도 {_status_bytes(legacy.get('project_limit_bytes'))} · "
          f"{_status_bytes(legacy.get('run_limit_bytes'))}/run")
    print(f"legacy terminal: {terminal_observed}{terminal_bytes} · "
          f"명령당 {_status_bytes(legacy.get('terminal_limit_bytes'))}")

    segmented_state = segmented.get("state")
    segmented_label = segmented_state if isinstance(segmented_state, str) else "unknown"
    raw_limit = segmented.get("raw_limit_bytes")
    if type(raw_limit) is not int or raw_limit < 0:
        raw_limit = SEGMENT_LIMIT * SEGMENT_COUNT
    if segmented_state == "not_allocated":
        print(f"segmented-v1 저장소: 미생성 · raw 예산 {_status_bytes(raw_limit)} · metadata 사용량: 미확인")
    else:
        print(f"segmented-v1 조회 상태: {segmented_label}")
        raw_bytes = _status_bytes(segmented.get("raw_bytes"))
        metadata_bytes = _status_bytes(segmented.get("metadata_bytes"))
        if segmented_state == "available":
            print(f"segmented-v1 raw: {raw_bytes} / {_status_bytes(raw_limit)} · metadata: {metadata_bytes}")
        else:
            print(f"segmented-v1 raw 사용량: {'확인된 ' if raw_bytes != '미확인' else ''}{raw_bytes} · "
                  f"예산 {_status_bytes(raw_limit)} · metadata 사용량: {metadata_bytes}")
        reason = segmented.get("reason")
        if isinstance(reason, str) and reason:
            print(f"segmented-v1 조회 사유: {_plain(reason)}")

    print("이 표시는 legacy, important DB, 기타 파일을 합친 전체 용량 상한이 아닙니다")
    print("중요 로그 DB 상태: logs important status")


def cmd_raw_read(args: argparse.Namespace) -> int:
    """User-only bounded source read; stopped queries never start writer recovery."""
    from workbench.storage.log_raw.port import raw_query, raw_usage
    try:
        layout = _layout(args, create=False)
        if args.logs_kind == "raw-status":
            try:
                with _verified_client(layout, name="workbench-cli", timeout=5) as client:
                    answer = client.request(ClientType.RAW_STATUS)
                    result = answer["result"] if answer.get("ok") else {"status": "unavailable",
                                                                     "reason": answer.get("reason")}
            except (NotRunning, ClientError, OSError):
                result = raw_usage(layout.root)
                from workbench.backend.raw_rotation import RawRotationPolicy
                from workbench.backend.summary_compatibility import SummaryCompatibility
                result["rotation"] = RawRotationPolicy(layout.raw_rotation,
                    SummaryCompatibility(layout.summary_compatibility,
                        omp_identity=current_omp_identity, boundary=compatibility_boundary)).status()
                result["activation"] = "enabled" if result["rotation"]["activated"] else "disabled"
        else:
            kind, identity = ("run", args.run_id) if args.run_id else ("command", args.command_id)
            try:
                with _verified_client(layout, name="workbench-cli", timeout=5) as client:
                    answer = client.request(ClientType.RAW_QUERY, source_kind=kind, source_id=identity,
                                            offset=args.offset, limit=args.limit)
                    result = answer["result"] if answer.get("ok") else {"status": "unavailable",
                                                                     "reason": answer.get("reason")}
            except (NotRunning, ClientError, OSError):
                result = raw_query(layout.root, kind, identity, offset=args.offset, limit=args.limit)
        if args.json:
            print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        elif args.logs_kind == "raw-status":
            _raw_status_print(result)
        elif result.get("log_format") == "segmented-v1":
            read = result.get("read") or {}
            for chunk in read.get("chunks", []):
                span = chunk["range"]
                print(f"[{span['start']},{span['end']}) retained:")
                sys.stdout.buffer.write(base64.b64decode(chunk["dataBase64"]))
                print()
            for gap in read.get("missing", []):
                print(f"[{gap['start']},{gap['end']}) {gap['reason']}", file=sys.stderr)
            if result.get("next_offset") is not None:
                print(f"next offset: {result['next_offset']}", file=sys.stderr)
        elif result.get("status") == "available":
            sys.stdout.buffer.write(base64.b64decode(result["read"]["dataBase64"]))
        else:
            print(f"raw 조회 불가: {result.get('reason', 'unknown')}", file=sys.stderr)
        return 0 if result.get("status") not in ("unavailable", "unknown") else EXIT_FAILURE
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"raw 조회 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


def cmd_important_mutate(args: argparse.Namespace) -> int:
    from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogReader
    from workbench.storage.important_log.store import ImportantLogStoreError, _canonical_uuid
    from workbench.contracts.logs_v1 import parse_subject_ref

    try:
        layout = _layout(args, create=False)
        if args.logs_action == "delete-db":
            identity = _canonical_uuid(args.db)
            before = ImportantLogReader(layout.important_logs).status()
            if not before.get("complete"):
                raise ImportantLogStoreError("archive inventory unavailable; deletion refused")
            selected = next((row for row in before["databases"] if row["dbUuid"] == identity), None)
            if selected is None or selected["role"] != "archive":
                raise ImportantLogStoreError("only a selected catalog archive can be deleted")
            print(f"선택 archive {identity} · {selected['physicalBytes']} bytes; 보호 시작/누적 기록은 "
                  "이월·재검증 후 삭제합니다. 실패 시 실제 잔존을 다시 확인하세요", file=sys.stderr)
            description = f"중요 로그 archive {identity} 삭제"
        else:
            subject = parse_subject_ref(_important_subject(args))
            description = f"{subject['kind']} {subject['id']} 보관 종결 (실행 상태·성공 판정 불변)"
        if not _important_confirmation(args, description):
            return EXIT_FAILURE
        # A connected backend owns the instance and its catalog. Never fall
        # back to a local retry after sending an unknown-result mutation.
        running = _running_snapshot(layout) if os.path.lexists(layout.root) else None
        if running is not None:
            with _verified_client(layout, name="workbench-cli", timeout=60) as client:
                if args.logs_action == "delete-db":
                    answer = client.request(ClientType.IMPORTANT_DELETE_DB, db_uuid=identity, confirmed=True)
                else:
                    answer = client.request(ClientType.IMPORTANT_ARCHIVE_CLOSE,
                                            subject_kind=subject["kind"], subject_id=subject["id"],
                                            confirmed=True)
            if not answer.get("ok"):
                raise RuntimeError(f"backend refused: {answer.get('reason')}: {answer.get('detail')}")
            result = answer.get("result")
        else:
            if not os.path.isdir(layout.root):
                raise ImportantLogStoreError("existing private data directory required")
            lock = InstanceLock(layout.lock)
            lock.acquire()
            try:
                catalog = ImportantLogCatalog(layout.important_logs, create=False)
                if args.logs_action == "delete-db":
                    result = catalog.prepare_and_delete_archive(identity)
                else:
                    ActiveSubjectLog(catalog).archive_close(
                        subject, at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        user_confirmed=True)
                    result = {"state": "closed", "subject": subject,
                              "note": "archival closure does not change core execution or task status"}
            finally:
                lock.release()
        if type(result) is not dict:
            raise RuntimeError("mutation result unknown; inspect status before retrying")
        if args.json:
            print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        else:
            print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0 if result.get("state") in ("closed", "deleted") else EXIT_FAILURE
    except (BackendLocked, ClientError, OSError, ValueError, RuntimeError) as exc:
        print(f"important 변경 결과 불명 또는 거부: {type(exc).__name__}: {exc}; "
              "'logs important status'로 잔존 상태를 확인하세요", file=sys.stderr)
        return EXIT_FAILURE


def _worktrees(layout: DataLayout) -> list[str]:
    try:
        return sorted(entry.name for entry in os.scandir(layout.workflow / "worktrees"))[:20]
    except OSError:
        return []


def _print_boot_conditions(layout: DataLayout, snapshot: dict, stream=None) -> None:
    """C-AC-23: the environment and execution conditions shown before the user confirms the boot."""
    stream = stream or sys.stdout
    boot, backend = snapshot.get("boot") or {}, snapshot.get("backend") or {}
    shell = ((snapshot.get("panes") or {}).get("host_shell") or {}).get("shell") or {}
    isolation = snapshot.get("omp_isolation") or {}
    automation = snapshot.get("automation") or {}
    task = snapshot.get("task") or {}
    print("== 부팅 확인: 환경·실행 조건 ==", file=stream)
    print(f"boot marker: 기록 {boot.get('recorded_boot_id')} -> 현재 {boot.get('boot_id')} "
          f"(사유 {boot.get('reason')})", file=stream)
    print(f"data dir: {backend.get('data_dir')}", file=stream)
    print(f"project dir: {backend.get('project_dir')}", file=stream)
    print(f"host shell: {shell.get('kind')} {shell.get('executable')} (mode {shell.get('parent_mode')})", file=stream)
    print(f"OMP: {backend.get('omp_version')} | 격리 확인: {isolation.get('state')}"
          + (f" ({isolation.get('warning')})" if isolation.get("warning") else ""), file=stream)
    print(f"자동화: {automation.get('state')}" + (" (일시정지 유지)" if automation.get("paused") else ""), file=stream)
    if task:
        print(f"Task: {task.get('kind')} {task.get('task_id')} 상태 {task.get('status')}"
              + (f" (보류 사유 {task.get('held_reason')})" if task.get("held_reason") else "")
              + (f' "{task.get("summary")}"' if task.get("summary") else ""), file=stream)
    worktrees = _worktrees(layout)
    if worktrees:
        print(f"worktree: {', '.join(worktrees)}", file=stream)
    _print_recovery({**snapshot, "boot": {}}, stream)
    print("확인하면 Workbench 자동 동작(worker 지시 전달, worker terminal, 감시·복구 알림, 60초 점검, 새 실험)이 다시 "
          "허용됩니다. 재부팅 전 실험은 다시 실행되지 않습니다.", file=stream)


def cmd_confirm_boot(args: argparse.Namespace) -> int:
    layout = _layout(args, create=False)
    snapshot = _running_snapshot(layout) if os.path.lexists(layout.root) else None
    if snapshot is None:
        print(f"no backend is running for {layout.root}; use 'start'", file=sys.stderr)
        return EXIT_NOT_RUNNING
    boot = snapshot.get("boot") or {}
    if not boot.get("confirmation_required"):
        print("부팅 확인이 필요하지 않습니다 (대기 중인 확인 없음)", file=sys.stderr)
        return EXIT_FAILURE
    _print_boot_conditions(layout, snapshot)
    boot_id = boot.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id:
        print("현재 boot marker를 읽을 수 없어 확인할 수 없습니다; 자동 동작은 계속 보류됩니다", file=sys.stderr)
        return EXIT_FAILURE
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing to confirm the boot without confirmation (use --yes)", file=sys.stderr)
            return EXIT_FAILURE
        if input("이 환경으로 계속할까요? [y/N] ").strip().lower() != "y":
            print("cancelled")
            return EXIT_FAILURE
    try:
        with _verified_client(layout, name="workbench-cli", timeout=10) as client:
            answer = client.request(ClientType.CONFIRM_BOOT, boot_id=boot_id)
    except (NotRunning, ClientError, OSError) as exc:
        print(f"confirm-boot failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    if not answer.get("ok"):
        print(f"confirm-boot refused: {answer.get('reason')}: {answer.get('detail')}", file=sys.stderr)
        return EXIT_FAILURE
    if args.json:
        print(json.dumps({"confirmed": True, "boot": answer.get("boot")}, sort_keys=True))
    else:
        print(f"부팅 확인됨 ({boot_id}): Workbench 자동 동작이 다시 허용됩니다")
    return 0


SHUTDOWN_RESULT_WAIT = 150.0  # used when an older backend does not send its own bound (``result_deadline``)
SHUTDOWN_PROGRESS_EVERY = 5.0  # seconds between the human form's progress lines (stderr)
BACKEND_GONE_WAIT = 10.0  # after a lost connection: how long to watch the backend's exact pid end


def _backend_state(ref: object, wait: float) -> str:
    """``ended``, ``alive`` or ``unknown`` for the backend's exact pid and start ticks (never signalled)."""
    if not isinstance(ref, Mapping) or type(ref.get("pid")) is not int or type(ref.get("start_ticks")) is not int:
        return "unknown"
    from workbench.app.recovery import observe
    deadline = time.monotonic() + wait
    while True:
        state = observe(ref["pid"], ref["start_ticks"])
        if state != "alive" or time.monotonic() >= deadline:
            return state
        time.sleep(0.1)


class ShutdownWait(NamedTuple):
    """``confirm_shutdown``: the confirm's refusal (the ``ok: false`` reply), or the ``closing``
    frame's ``result`` (None when none came) and why none came (``lost``)."""
    refused: dict | None
    result: dict | None
    lost: str | None


def confirm_shutdown(client: UiClient, token: str, *, progress=None) -> ShutdownWait:
    """Send ``shutdown_confirm`` and wait for the backend's ``closing`` result until its own
    ``result_deadline`` (``SHUTDOWN_RESULT_WAIT`` for an older backend). Shared by ``wb shutdown``
    and ``workbench.hub.projects.power_off``. A lost connection, a timeout or Ctrl-C while
    waiting leaves ``result`` None (never a success, C-AC-22). ``progress`` (a text stream, or
    None for none) gets the human progress lines."""
    wait = SHUTDOWN_RESULT_WAIT
    lost: str | None = None
    try:
        try:
            confirmed = client.request(ClientType.SHUTDOWN_CONFIRM, token=token)
        except TimeoutError:  # an older backend answers only when its close ends: keep waiting for the result
            confirmed = {"ok": True}
        except (OSError, ClientError) as exc:
            confirmed = {"ok": True}
            lost = f"{type(exc).__name__}: {exc}"
        if not confirmed.get("ok"):
            return ShutdownWait(confirmed, None, None)
        bound = confirmed.get("result_deadline")
        if type(bound) in (int, float) and bound > 0:
            wait = float(bound)
        started = time.monotonic()
        deadline, next_progress = started + wait, started + SHUTDOWN_PROGRESS_EVERY
        if progress is not None and client.closing is None and lost is None:
            print(f"종료 중: 진행 중인 OMP turn 중단과 process 정리를 기다립니다 (최대 {wait:.0f}초)",
                  file=progress)
        while client.closing is None and lost is None:
            now = time.monotonic()
            if now >= deadline:
                lost = f"no result within {wait:.0f} s"
                break
            if progress is not None and now >= next_progress:
                print(f"종료 중… {now - started:.0f}초 경과", file=progress)
                next_progress = now + SHUTDOWN_PROGRESS_EVERY
            try:
                if not client.pump(min(0.2, deadline - now)):
                    lost = "the backend closed the connection before its result"
            except (OSError, ClientError) as exc:
                lost = f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:  # fix-05 (C17 P3-2): the wait ended by the user; the stop was requested, not confirmed
        lost = "interrupted (Ctrl-C) while waiting for the result"
    result = (client.closing or {}).get("result")
    return ShutdownWait(None, result if isinstance(result, dict) else None, lost)


def cmd_shutdown(args: argparse.Namespace) -> int:
    """C-AC-22: show the active work, confirm, then wait for the backend's own result; a result that is not
    verified, or none at all, is never shown as success, and no failure ends in a traceback (CW-16 O4)."""
    layout = _layout(args)
    try:
        client = _verified_client(layout, name="workbench-cli", timeout=10)
    except NotRunning:
        print(f"no backend is running for {layout.root}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    except (OSError, ClientError) as exc:
        print(f"backend did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    backend_ref: object = None
    with client:
        try:
            pending = client.request(ClientType.SHUTDOWN_REQUEST)
        except (OSError, TimeoutError, ClientError) as exc:
            print(f"shutdown request failed (nothing was stopped): {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        except KeyboardInterrupt:  # C18 review P3: Ctrl-C before the confirmation stops nothing
            print("\ncancelled (nothing was stopped)", file=sys.stderr)
            return EXIT_FAILURE
        if not pending.get("ok", True) or "token" not in pending:
            print(f"shutdown request refused: {pending.get('reason')}: {pending.get('detail')}", file=sys.stderr)
            return EXIT_FAILURE
        backend_ref = (pending.get("processes") or {}).get("backend")
        active = pending.get("active", [])
        print("active work:" if active else "no active work reported")
        for item in active:
            print(f"  - {json.dumps(item, sort_keys=True)}")
        if not args.yes:
            # fix-05 (C17 P3-2): no input() on a closed or non-tty stdin (no EOFError/AttributeError traceback).
            stdin = sys.stdin
            interactive = False
            try:
                interactive = stdin is not None and stdin.isatty()
            except (OSError, ValueError):
                pass
            if not interactive:
                print("refusing to shut down without confirmation: confirm with --yes (nothing was stopped). "
                      "종료하려면 --yes를 붙여 다시 실행하세요", file=sys.stderr)
                return EXIT_FAILURE
            try:
                answer = input("stop the backend, both OMP sessions and the host shell? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                print("\ncancelled (nothing was stopped; use --yes to confirm)", file=sys.stderr)
                return EXIT_FAILURE
            if answer.strip().lower() != "y":
                print("cancelled")
                return EXIT_FAILURE
        refused, result, lost = confirm_shutdown(client, pending["token"], progress=None if args.json else sys.stderr)
        if refused is not None:
            print(f"shutdown refused: {refused.get('reason')}: {refused.get('detail')}", file=sys.stderr)
            return EXIT_FAILURE
    if not isinstance(result, dict):
        result = None
    if result is None:
        # C-AC-22: no result is never a success; say whether the backend itself is gone (exact pid identity).
        backend = _backend_state(backend_ref, BACKEND_GONE_WAIT)
        cause = lost or "the backend sent no result"
        backend_text = {"ended": "backend process는 종료됨", "alive": "backend process가 아직 실행 중",
                        "unknown": "backend process 상태 불명"}[backend]
        if args.json:
            print(json.dumps({"shutdown": None, "unconfirmed": {"cause": cause, "backend": backend}},
                             sort_keys=True))
        else:
            print("shutdown result: null")
        print(f"종료 확인 실패: 종료 결과를 받지 못해 OMP·host shell 등의 종료를 확인할 수 없습니다 "
              f"({backend_text}; {cause}). 'status'로 확인하세요", file=sys.stderr)
        return EXIT_FAILURE
    if args.json:
        print(json.dumps({"shutdown": result}, sort_keys=True))
    else:
        print(f"shutdown result: {json.dumps(result, sort_keys=True)}")
        if not result.get("verified"):  # C-AC-22: never shown as success
            print("종료 확인 실패: 일부 process의 종료를 확인하지 못했습니다 "
                  f"({', '.join(result.get('problems') or ['result unknown'])})", file=sys.stderr)
    return 0 if result.get("verified") else EXIT_FAILURE


def cmd_backend(args: argparse.Namespace) -> int:
    from workbench.backend.service import main_backend
    return main_backend(args)


def cmd_setup(args) -> int:
    from workbench.backend.setup import SetupError, run_setup
    try:
        result = run_setup(args.omp)
    except (SetupError, OSError, ValueError, KeyboardInterrupt) as error:
        result = {"state": "failed", "status": "failed", "reason": "interrupted" if isinstance(error, KeyboardInterrupt) else getattr(error, "code", type(error).__name__),
                  "path": getattr(error, "path", None) or args.omp,
                  "detail": getattr(error, "detail", None),
                  "fix": getattr(error, "fix", None),
                  "retry": "Fix the reported OMP/path/network issue and retry wb setup."}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            retry = f"Fix: {result['fix']}, then retry wb setup." if result["fix"] else result["retry"]
            print(f"OMP setup failed: {result['reason']} ({result['path'] or 'user program prefix'}): {result['detail'] or ''}. {retry}")
        return EXIT_FAILURE
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"OMP {result['status']}: {result['path']} ({result['version']}); SDK loaded; real models unverified.")
        link = result['command_link']
        print(f"OMP command link {link['status']}: {link['path']}")
        if link['warning']:
            print(f"Warning: {link['warning']}")
        if not link['plain_wb_ready']:
            fix = f" Fix: {link['fix']}, then" if link['fix'] else " Resolve this, then"
            print(f"Note: plain `wb` will not start until {link['path']} works.{fix} run wb setup again. "
                  f"To run once without it: WORKBENCH_OMP={result['path']} wb")
        print(link['guidance'])
        print(result['login_guidance'])
        print(result['update_guidance'])
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from workbench.backend.migrate import migrate
    result = migrate(Path(args.source), Path(args.project), os.environ)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from workbench.backend.update import run
    return run(args)


def cmd_doctor(args: argparse.Namespace) -> int:
    from workbench.backend.doctor import run
    return run(args)


def cmd_hub_status(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_status as run
    return run(args)


def cmd_hub_stop(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_stop as run
    return run(args)


def cmd_hub_start(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_start as run
    return run(args)


def cmd_hub_list(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_list as run
    return run(args)


def cmd_hub_enable(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_enable as run
    return run(args)


def cmd_hub_disable(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_disable as run
    return run(args)


def cmd_hub_tls(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_tls as run
    return run(args)


def cmd_hub_reload(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_reload as run
    return run(args)


def cmd_hub_review_mode(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_review_mode as run
    return run(args)


def cmd_hub_passwd(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_passwd as run
    return run(args)


def cmd_hub_add(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_add as run
    return run(args)


def cmd_hub_remove(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_hub_remove as run
    return run(args)


def cmd_init(args: argparse.Namespace) -> int:
    from workbench.hub.cli import cmd_init as run
    return run(args)


def _positive(kind):
    def parse(text: str):
        value = kind(text)
        if not value > 0:
            raise argparse.ArgumentTypeError(f"must be greater than 0: {text}")
        return value
    parse.__name__ = kind.__name__
    return parse


def cmd_hub_process(args: argparse.Namespace) -> int:
    from workbench.hub.server import main_hub
    return main_hub(args)


class _VersionAction(argparse.Action):
    """``--version`` printed lazily (distribution metadata is read only when asked)."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        from workbench.hub.cli import version_line
        print(version_line())
        parser.exit()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="wb", description=__doc__.splitlines()[0])
    root.add_argument("--version", action=_VersionAction, help="wb 버전을 표시하고 끝냄")
    sub = root.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--data-dir", help="data dir (default: $WORKBENCH_DATA_DIR or nearest project .wb/)")
        return p

    update = common(sub.add_parser('update', help='확인·백업 후 현재 uv Git 설치 업데이트'))
    update.add_argument('--ref', default='main', help='branch, tag 또는 commit (기본 main)')
    update.set_defaults(handler=cmd_update)

    doctor = common(sub.add_parser('doctor', help='읽기 전용 로컬 상태 진단'))
    doctor.add_argument('--json', action='store_true')
    doctor.set_defaults(handler=cmd_doctor)

    migration = sub.add_parser("migrate", help="copy a stopped legacy store to an absent project .wb")
    migration.add_argument("--from", dest="source", required=True)
    migration.add_argument("--project", required=True)
    migration.set_defaults(handler=cmd_migrate)
    setup = sub.add_parser("setup", help="prepare a shared user OMP installation")
    setup.add_argument("--omp", help="existing OMP executable (before WORKBENCH_OMP, saved setup and PATH)")
    setup.add_argument("--json", action="store_true")
    setup.set_defaults(handler=cmd_setup)
    start = common(sub.add_parser("start", help="start (or reuse) the backend and attach"))
    start.add_argument("--omp", help="OMP executable (default: $WORKBENCH_OMP or ~/.local/bin/omp)")
    start.add_argument("--omp-arg", action="append", help="extra argument passed to both OMP processes")
    start.add_argument("--bridge-extension", help="path to the G3 bridge extension")
    start.add_argument("--no-attach", action="store_true", help="start or reuse the backend without attaching")
    start.add_argument("--plain", action="store_true", help="attach with the minimal client instead of the product UI")
    start.add_argument("--timeout", type=float, default=START_TIMEOUT, help=argparse.SUPPRESS)
    start.set_defaults(handler=cmd_start)
    init = sub.add_parser("init", help="폴더를 WB 프로젝트로 초기화(확인 필요)하고 Hub에 등록; backend는 시작하지 않음")
    init.add_argument("path", nargs="?", metavar="PATH", help="초기화할 폴더 (기본: 현재 폴더)")
    init.set_defaults(handler=cmd_init)
    attach = common(sub.add_parser("attach", help="attach to the running backend"))
    attach.add_argument("--plain", action="store_true", help="use the minimal client instead of the product UI")
    attach.set_defaults(handler=cmd_attach)
    status = common(sub.add_parser("status", help="show backend status"))
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=cmd_status)
    confirm = common(sub.add_parser("confirm-boot", help="show the conditions after a reboot and confirm the boot"))
    confirm.add_argument("--yes", action="store_true", help="confirm without prompting")
    confirm.add_argument("--json", action="store_true")
    confirm.set_defaults(handler=cmd_confirm_boot)
    shutdown = common(sub.add_parser("shutdown", aliases=["down"], help="request and confirm a full backend shutdown"))
    shutdown.add_argument("--yes", action="store_true", help="confirm without prompting")
    shutdown.add_argument("--json", action="store_true")
    shutdown.set_defaults(handler=cmd_shutdown)
    logs = sub.add_parser("logs", help="inspect stored logs and perform explicit user cleanup")
    logs_sub = logs.add_subparsers(dest="logs_kind", required=True)
    important = logs_sub.add_parser("important", help="important task/command logs")
    important_sub = important.add_subparsers(dest="logs_action", required=True)

    def important_read(name: str) -> argparse.ArgumentParser:
        action = common(important_sub.add_parser(name))
        action.add_argument("--json", action="store_true")
        action.set_defaults(handler=cmd_important_read)
        return action

    def subject_argument(action: argparse.ArgumentParser) -> None:
        subject = action.add_mutually_exclusive_group(required=True)
        subject.add_argument("--task", help="canonical Task UUID")
        subject.add_argument("--command", dest="command_id", help="canonical command UUID")

    important_read("status")
    listed = important_read("list")
    listed.add_argument("--limit", type=int, default=50)
    listed.add_argument("--page", help="cursor returned as nextAfter")
    shown = important_read("show")
    subject_argument(shown)
    shown.add_argument("--limit", type=int, default=50)
    shown.add_argument("--page", help="cursor returned as nextAfter")
    archival = common(important_sub.add_parser("archival-close", help="mark a subject as archivally closed"))
    subject_argument(archival)
    archival.add_argument("--yes", action="store_true")
    archival.add_argument("--json", action="store_true")
    archival.set_defaults(handler=cmd_important_mutate)
    deletion = common(important_sub.add_parser("delete-db", help="delete one selected archive UUID"))
    deletion.add_argument("--db", required=True, help="canonical archive UUID")
    deletion.add_argument("--yes", action="store_true")
    deletion.add_argument("--json", action="store_true")
    deletion.set_defaults(handler=cmd_important_mutate)
    raw = common(logs_sub.add_parser("raw", help="read retained raw source bytes"))
    raw_identity = raw.add_mutually_exclusive_group(required=True)
    raw_identity.add_argument("--run", dest="run_id", help="canonical run UUID")
    raw_identity.add_argument("--command", dest="command_id", help="canonical command UUID")
    raw.add_argument("--offset", type=int, default=0)
    raw.add_argument("--limit", type=int, default=16 * 1024)
    raw.add_argument("--json", action="store_true")
    raw.set_defaults(handler=cmd_raw_read)
    raw_status = common(logs_sub.add_parser("raw-status", help="show separate legacy and disabled v2 usage"))
    raw_status.add_argument("--json", action="store_true")
    raw_status.set_defaults(handler=cmd_raw_read)
    rotation = logs_sub.add_parser("raw-rotation", help="사용자 전용 raw rotation 활성화")
    rotation_sub = rotation.add_subparsers(dest="rotation_action", required=True)
    activation = common(rotation_sub.add_parser("activate", help="검증된 로컬 제품 record로 명시 활성화"))
    activation.add_argument("--yes", action="store_true")
    activation.add_argument("--evidence", required=True, help="로컬 product record UUID (파일 경로 아님)")
    activation.set_defaults(handler=cmd_raw_rotation)
    compat = logs_sub.add_parser("summary-compat", help="중요 로그 요약 호환 상태와 명시적 검증")
    compat_sub = compat.add_subparsers(dest="compat_action", required=True)
    compat_status = common(compat_sub.add_parser("status", help="로컬 호환 상태 표시"))
    compat_status.add_argument("--json", action="store_true")
    compat_status.set_defaults(handler=cmd_summary_compat)
    compat_verify = common(compat_sub.add_parser("verify", help="명시적 scripted 검증; 요약·raw 활성화 없음"))
    compat_verify.add_argument("--scripted", action="store_true", required=True)
    compat_verify.add_argument("--json", action="store_true")
    compat_verify.set_defaults(handler=cmd_summary_compat)
    records = sub.add_parser("records", help="read saved work records")
    records_sub = records.add_subparsers(dest="records_action", required=True)

    def records_read(name: str) -> argparse.ArgumentParser:
        action = common(records_sub.add_parser(name))
        action.add_argument("--json", action="store_true")
        action.set_defaults(handler=_records_command)
        return action

    listed_records = records_read("list")
    listed_records.add_argument("--task", help="filter by Task ID")
    listed_records.add_argument("--limit", type=int, default=20)
    listed_records.add_argument("--page", help="cursor returned as next_after")
    searched_records = records_read("search")
    searched_records.add_argument("query")
    searched_records.add_argument("--task", help="filter by Task ID")
    searched_records.add_argument("--limit", type=int, default=20)
    searched_records.add_argument("--page", help="cursor returned as next_after")
    shown_record = records_read("show")
    shown_record.add_argument("record_id")
    shown_record.add_argument("--revision", type=int)
    history_records = records_read("history")
    history_records.add_argument("record_id")
    history_records.add_argument("--limit", type=int, default=20)
    history_records.add_argument("--page", help="cursor returned as next_after")

    export_records = records_read("export")
    export_records.add_argument("record_id")
    export_records.add_argument("--revision", type=int)
    export_records.add_argument("--output", required=True, help="explicit Markdown output path (never overwritten)")

    hub = sub.add_parser("hub", help="사용자당 하나인 Workbench Hub 관리")
    hub_sub = hub.add_subparsers(dest="hub_action", required=True)
    hub_start = hub_sub.add_parser("start", help="Hub가 없으면 background로 시작")
    hub_start.set_defaults(handler=cmd_hub_start)
    hub_status = hub_sub.add_parser("status", help="Hub 상태 표시")
    hub_status.add_argument("--json", action="store_true")
    hub_status.set_defaults(handler=cmd_hub_status)
    hub_stop = hub_sub.add_parser("stop", help="Hub 정지 (WB 프로젝트 backend는 계속 실행)")
    hub_stop.add_argument("--yes", action="store_true", help="확인 없이 정지")
    hub_stop.set_defaults(handler=cmd_hub_stop)
    hub_list = hub_sub.add_parser("list", help="등록된 WB 프로젝트 목록과 상태")
    hub_list.add_argument("--json", action="store_true", help="hub_v1 list 결과 그대로")
    hub_list.set_defaults(handler=cmd_hub_list)
    hub_sub.add_parser("enable", help="로그인(linger 시 부팅) 뒤 Hub를 systemd 사용자 서비스로 자동 시작") \
        .set_defaults(handler=cmd_hub_enable)
    hub_sub.add_parser("disable", help="부팅 자동 시작 unit을 끄고 지움 (실행 중인 Hub는 유지)") \
        .set_defaults(handler=cmd_hub_disable)
    hub_sub.add_parser("reload", help="실행 중인 Hub가 hub.toml을 다시 읽음 (웹 listener만 다시 열고 WB 프로젝트는 건드리지 않음)") \
        .set_defaults(handler=cmd_hub_reload)
    hub_passwd = hub_sub.add_parser("passwd", help="Web Board 로그인 비밀번호 설정 (terminal에서 두 번 입력)")
    hub_passwd.add_argument("--stdin", action="store_true", help="stdin 첫 줄을 비밀번호로 읽음 (스크립트용)")
    hub_passwd.set_defaults(handler=cmd_hub_passwd)
    hub_tls = hub_sub.add_parser("tls", help="Web Board 용 자체 서명 TLS 인증서·key 생성 (host openssl)")
    hub_tls.add_argument("--self-signed", action="store_true", help="자체 서명 인증서를 hub.toml의 TLS 경로에 만듦")
    hub_tls.add_argument("--force", action="store_true", help="이미 있는 cert·key를 새로 만든 것으로 바꿈")
    hub_tls.set_defaults(handler=cmd_hub_tls)
    hub_add = hub_sub.add_parser("add", help="WB 프로젝트 root를 Hub registry에 등록")
    hub_add.add_argument("path", metavar="PATH")
    hub_add.set_defaults(handler=cmd_hub_add)
    hub_remove = hub_sub.add_parser("remove", help="Hub registry에서 등록 해제 (.wb는 그대로)")
    hub_remove.add_argument("path", metavar="PATH")
    hub_remove.set_defaults(handler=cmd_hub_remove)
    hub_review = hub_sub.add_parser("review-mode", help="WB 프로젝트의 검토 방식(auto|manual) 보기·바꾸기")
    hub_review.add_argument("path", metavar="PATH")
    hub_review.add_argument("mode", nargs="?", choices=("auto", "manual"))
    hub_review.set_defaults(handler=cmd_hub_review_mode)

    backend = sub.add_parser("_backend", help=argparse.SUPPRESS)
    backend.add_argument("--data-dir", required=True)
    backend.add_argument("--project-dir", required=True)
    backend.add_argument("--runtime-root", help=argparse.SUPPRESS)
    backend.add_argument("--startup-id", help=argparse.SUPPRESS)
    backend.add_argument("--shell-kind", required=True, choices=("bash", "sh"))
    backend.add_argument("--shell-path", required=True)
    backend.add_argument("--omp", required=True)
    backend.add_argument("--omp-version", required=True)
    backend.add_argument("--bridge-extension", required=True)
    backend.add_argument("--omp-arg", action="append")
    backend.set_defaults(handler=cmd_backend)
    hub_process = sub.add_parser("_hub", help=argparse.SUPPRESS)
    hub_process.add_argument("--foreground", action="store_true")
    hub_process.add_argument("--mode", choices=("spawned", "foreground", "systemd"))
    # Policy injection for tests only (SPEC defaults otherwise).
    hub_process.add_argument("--stop-token-ttl", type=_positive(int), default=60, help=argparse.SUPPRESS)
    hub_process.add_argument("--idle-timeout", type=_positive(float), default=60.0, help=argparse.SUPPRESS)
    # Set by spawn_hub: the ready pipe (R/L/M) and the intermediate to outlive.
    hub_process.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    hub_process.add_argument("--spawner-pid", type=int, help=argparse.SUPPRESS)
    hub_process.set_defaults(handler=cmd_hub_process)
    return root


HUB_LIST_NEEDS_TTY = "Hub 목록은 terminal이 필요합니다. 'wb hub list'를 쓰세요"


def _bare_outside_project() -> bool:
    """True when a bare ``wb`` runs outside a WB project: no ``.wb`` above the cwd and no
    ``WORKBENCH_DATA_DIR``. A marker that cannot be read stays "inside" so ``start`` reports it."""
    from workbench.backend.project import resolve_project_context
    try:
        return resolve_project_context(Path.cwd(), os.environ).needs_initialization
    except (DataDirError, OSError, ValueError):
        return False


def cmd_hub_tui(args: argparse.Namespace) -> int:
    from workbench.ui.hub_list.app import run_hub_list
    return run_hub_list()


def main(argv: list[str] | None = None, *, autostart: bool | None = None) -> int:
    """``autostart`` (default: only for the real entrypoint, ``argv is None``) starts
    the Hub after argparse and before the handler (SPEC "자동 시작 규칙"). In-process
    callers that pass ``argv`` never spawn a background Hub unless they ask."""
    values = list(sys.argv[1:] if argv is None else argv)
    if values == ["hub"] or (not values and _bare_outside_project()):
        # Bare `wb` outside a project, and `wb hub` with no subcommand from any cwd: the Hub list
        # (it needs the Hub, so `hub list` auto-starts it).
        if not (sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()):
            print(HUB_LIST_NEEDS_TTY, file=sys.stderr)
            return EXIT_REQUIREMENT
        args = parser().parse_args(["hub", "list"])
        args.handler = cmd_hub_tui
    else:
        args = parser().parse_args(values or ["start"])
    if autostart is None:
        autostart = argv is None
    if autostart:
        from workbench.hub.cli import autostart as start_hub
        start_hub(args)
    try:
        return args.handler(args)
    except DataDirError as exc:
        print(f"data dir error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
