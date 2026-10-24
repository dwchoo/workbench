"""``wb hub status/stop/add/remove`` and ``wb init`` handlers (other ``wb hub`` commands come later)."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from workbench.backend.paths import DataDirError

from . import registry
from .client import HubClient
from .paths import hub_layout
from .process import HubNotRunning, HubUnknown, installed_version

EXIT_OK, EXIT_FAILURE, EXIT_REQUIREMENT, EXIT_NOT_RUNNING = 0, 1, 2, 3
NOT_RUNNING_TEXT = "Hub 실행 중 아님"


def _version_text(version: object) -> str:
    if not isinstance(version, dict):
        return "unknown"
    release, commit, kind = version.get("release"), version.get("commit"), version.get("kind")
    detail = commit[:12] if kind == "git" and isinstance(commit, str) else kind
    return f"{release} ({detail})"


def _print_json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def cmd_hub_status(args: argparse.Namespace) -> int:
    layout = hub_layout()
    try:
        with HubClient(layout) as client:
            reply = client.request("status")
    except HubNotRunning:
        if args.json:
            _print_json({"state": "not_running", "detail": NOT_RUNNING_TEXT})
        else:
            print(NOT_RUNNING_TEXT)
        return EXIT_NOT_RUNNING
    except HubUnknown as exc:
        pid = (exc.record or {}).get("pid")
        if args.json:
            _print_json({"state": "unknown", "reason": exc.reason, "pid": pid, "lock": str(layout.lock)})
        else:
            print(f"Hub 상태 불명: {exc.reason} (lock {layout.lock}, record pid {pid})", file=sys.stderr)
        return EXIT_FAILURE
    if not reply.get("ok"):
        print(f"Hub status 실패: {reply.get('reason')}: {reply.get('detail')}", file=sys.stderr)
        return EXIT_FAILURE
    status = {key: reply.get(key) for key in ("hub", "registry", "web", "board_agent")}
    local = installed_version()
    drift = status["hub"].get("version") != local
    if args.json:
        _print_json({"state": "running", **status, "version_drift": drift})
        return EXIT_OK
    hub, registry = status["hub"], status["registry"] or {}
    started = hub.get("started_at")
    started_text = (datetime.fromtimestamp(started).astimezone().isoformat(timespec="seconds")
                    if isinstance(started, (int, float)) else "unknown")
    print(f"Hub 실행 중: pid {hub.get('pid')}, mode {hub.get('mode')}")
    print(f"  version: {_version_text(hub.get('version'))}")
    if drift:
        print(f"  경고: 이 wb의 설치 version({_version_text(local)})과 다릅니다. 'wb hub stop' 뒤 다시 시작하세요.")
    print(f"  started_at: {started_text}")
    print(f"  socket: {hub.get('socket')}")
    print(f"  log: {hub.get('log')}")
    print(f"  registry: {registry.get('state')}, {registry.get('count')}개 ({registry.get('path')})")
    print("  부팅 자동 시작: unknown")
    print(f"  web: {(status['web'] or {}).get('state')}")
    return EXIT_OK


def _ask_active(args: argparse.Namespace):
    def ask(active: list) -> bool:
        print("Hub를 멈추면 다음도 끝납니다:")
        for item in active:
            print(f"  - {item}")
        if args.yes:
            return True
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("확인할 terminal이 없어 멈추지 않았습니다. '--yes'로 다시 실행하세요.", file=sys.stderr)
            return False
        try:
            return input("계속할까요? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False
    return ask


def cmd_hub_stop(args: argparse.Namespace) -> int:
    from .process import stop_hub
    result = stop_hub(_ask_active(args))
    if result.status == "stopped":
        if result.signalled:
            print(f"Hub(pid {result.pid})가 응답하지 않아 SIGTERM으로 멈췄습니다.")
        print("Hub를 멈췄습니다. 켜져 있던 WB 프로젝트는 계속 실행됩니다.")
        return EXIT_OK
    if result.status == "not_running":
        print(NOT_RUNNING_TEXT)
        return EXIT_NOT_RUNNING
    if result.status == "refused":
        print("Hub를 멈추지 않았습니다.", file=sys.stderr)
        return EXIT_FAILURE
    if result.status == "unknown":
        print(f"Hub 상태 불명이라 멈추지 않았습니다: {result.reason} (pid {result.pid}, lock {hub_layout().lock})",
              file=sys.stderr)
        return EXIT_FAILURE
    print(f"Hub(pid {result.pid})를 멈추지 못했습니다: {result.reason}", file=sys.stderr)
    return EXIT_FAILURE


# --- project registration: wb init, wb hub add/remove ---------------------------------------

def project_environment() -> dict:
    """Project lookup for these commands starts at the given PATH, never at ``WORKBENCH_DATA_DIR``."""
    return {key: value for key, value in os.environ.items() if key != "WORKBENCH_DATA_DIR"}


def _fail(text: str, code: int = EXIT_FAILURE) -> int:
    print(text, file=sys.stderr)
    return code


def _registry_failure(exc: registry.RegistryError) -> int:
    advice = ""
    if isinstance(exc, registry.RegistryMismatch):
        advice = " 'wb hub remove <PATH>' 뒤 'wb hub add <PATH>'로 다시 등록하세요."
    elif exc.reason == "registry_invalid":
        advice = " 파일을 고치거나 지운 뒤 다시 실행하세요. 이 명령은 파일을 바꾸지 않았습니다."
    return _fail(f"Hub registry 실패 ({exc.reason}): {exc.detail}.{advice}")


def register_quietly(root: Path, project_id: str, source: str) -> registry.EnsureResult | None:
    """Automatic registration: any failure is one warning line and never stops the caller."""
    try:
        return registry.ensure(hub_layout(), root, project_id, source)
    except registry.RegistryMismatch:
        print(f"경고: Hub registry에 {root}가 다른 project_id로 등록되어 있어 덮어쓰지 않았습니다. "
              f"'wb hub remove {root}' 뒤 'wb hub add {root}'로 다시 등록하세요.", file=sys.stderr)
    except registry.RegistryError as exc:
        print(f"경고: Hub registry에 등록하지 못했습니다 ({exc.reason}): {exc.detail}", file=sys.stderr)
    except (DataDirError, OSError) as exc:
        print(f"경고: Hub registry에 등록하지 못했습니다: {exc}", file=sys.stderr)
    return None


def _context_at(path_text: str):
    from workbench.backend.project import resolve_project_context
    return resolve_project_context(Path(path_text).expanduser(), project_environment())


def _describe_registration(result: registry.EnsureResult, root: Path) -> str:
    verb = "등록했습니다" if result.added else "이미 등록되어 있습니다"
    return f"{verb}: {root} (project_id {result.entry.project_id})"


def _register_context(context, source: str) -> int:
    try:
        result = registry.ensure(hub_layout(), context.project_root, context.project_id, source)
    except registry.RegistryError as exc:
        return _registry_failure(exc)
    print(_describe_registration(result, context.project_root))
    return EXIT_OK


def cmd_hub_add(args: argparse.Namespace) -> int:
    try:
        context = _context_at(args.path)
    except FileNotFoundError:
        return _fail(f"경로를 찾을 수 없습니다: {args.path}")
    requested = Path(args.path).expanduser().resolve()
    if context.needs_initialization:
        return _fail(f"WB 프로젝트가 아닙니다: {requested}. 먼저 'wb init {requested}'로 초기화하세요.")
    if context.project_root != requested:
        return _fail(f"{requested}는 WB 프로젝트의 하위 폴더입니다. 프로젝트 root: {context.project_root} "
                     f"('wb hub add {context.project_root}')")
    return _register_context(context, "add")


def _unavailable_state(root: Path, project_id: str) -> str:
    """``on``/``starting``/``unknown`` keep a project registered; anything else may be removed."""
    from workbench.backend.paths import DataLayout
    from workbench.backend.process_identity import BackendIdentityError, inspect_backend
    from workbench.backend.project import read_project_marker
    from workbench.backend.runtime_paths import resolve_runtime_root
    data = root / ".wb"
    if not os.path.lexists(data):
        return "off"
    try:
        marker = read_project_marker(data)
    except DataDirError:
        return "unknown" if _lock_held(data) else "mismatch"
    if marker["project_id"] != project_id:
        return "mismatch"
    layout = DataLayout(data, runtime_root=resolve_runtime_root(data, project_id, project_environment()))
    try:
        identity = inspect_backend(layout)
    except (BackendIdentityError, DataDirError):
        return "unknown"
    return {"running": "on", "starting": "starting"}.get(identity.state, "off")


def _lock_held(data: Path) -> bool:
    from workbench.backend.paths import InstanceLock
    try:
        return InstanceLock(data / "backend.lock").held_elsewhere()
    except OSError:
        return True


def cmd_hub_remove(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    try:
        entry = next((item for item in registry.read(hub_layout()) if item.root == str(root)), None)
    except registry.RegistryError as exc:
        return _registry_failure(exc)
    if entry is None:
        return _fail(f"등록되어 있지 않습니다: {root}")
    state = _unavailable_state(root, entry.project_id)
    if state in ("on", "starting", "unknown"):
        return _fail(f"{root} 프로젝트가 켜져 있거나 상태를 알 수 없어({state}) 등록을 해제하지 않았습니다. "
                     f"먼저 끄세요: 'wb shutdown'")
    try:
        registry.remove(hub_layout(), root)
    except registry.RegistryError as exc:
        return _registry_failure(exc)
    print(f"등록을 해제했습니다: {root} (.wb는 그대로 둡니다)")
    return EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize ``PATH`` (default cwd) after a terminal ``[y/N]`` confirmation, then register it."""
    from workbench.backend.project import initialize_project
    path_text = args.path or "."
    target = Path(path_text).expanduser()
    if target.exists() and not target.is_dir():  # fail before the prompt, not after a "y"
        return _fail(f"폴더가 아닙니다: {target.resolve()}. .wb를 만들지 않았습니다.")
    try:
        context = _context_at(path_text)
    except FileNotFoundError:
        return _fail(f"경로를 찾을 수 없습니다: {path_text}")
    requested = Path(path_text).expanduser().resolve()
    if not context.needs_initialization:
        if context.project_root != requested:
            return _fail(f"{requested}는 이미 WB 프로젝트의 하위 폴더입니다. 프로젝트 root: {context.project_root}. "
                         "중첩 초기화는 하지 않습니다.")
        print(f"이미 WB 프로젝트입니다: {requested}")
        return _register_context(context, "init")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _fail(f"초기화 확인에는 terminal이 필요합니다: {requested}. .wb를 만들지 않았습니다.", EXIT_REQUIREMENT)
    declined = []

    def confirm(root: Path) -> bool:
        try:
            answer = input(f"WB project {root}. Create {root / '.wb'}? [y/N] ").strip().lower() == "y"
        except EOFError:
            answer = False
        if not answer:
            declined.append(root)
        return answer

    try:
        created = initialize_project(context, confirm=confirm, interactive=True, environment=project_environment())
    except DataDirError as exc:
        if declined:
            return _fail(f"초기화하지 않았습니다: {requested}", EXIT_REQUIREMENT)
        raise
    if created.project_root != requested:  # an enclosing project appeared before the init lock was taken
        return _fail(f"{requested}는 WB 프로젝트의 하위 폴더입니다. 프로젝트 root: {created.project_root}. "
                     "중첩 초기화는 하지 않습니다.")
    print(f"초기화했습니다: {created.data_root}")
    result = register_quietly(created.project_root, created.project_id, "init")
    if result is not None:
        print(_describe_registration(result, created.project_root))
    return EXIT_OK
