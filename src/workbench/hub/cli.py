"""``wb hub status`` and ``wb hub stop`` handlers (other ``wb hub`` commands come later)."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import sys

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


MANAGED_TREE_TEXT = ("[workbench] WB가 관리하는 terminal 안에서는 Hub를 자동으로 시작하지 않습니다. "
                     "외부 terminal에서 'wb hub start'를 실행하세요.")
# reason proc_unreadable (e.g. a hidepid /proc): held fail-closed, reported apart from managed_tree.
PROC_UNREADABLE_AUTOSTART_TEXT = "[workbench] 프로세스 정보를 읽을 수 없어 Hub 자동 시작을 보류했습니다."
PROC_UNREADABLE_START_TEXT = ("[workbench] 프로세스 정보를 읽을 수 없어 Hub를 시작하지 않았습니다. "
                              "/proc 접근(예: hidepid)을 확인하세요.")


def _start_failure_text(reason: str) -> str:
    if reason == "update_in_progress":
        return "업데이트가 진행 중이라 Hub를 시작하지 않았습니다. 업데이트가 끝난 뒤 다시 실행하세요."
    return f"Hub를 시작하지 못했습니다: {reason} (log {hub_layout().log})"


AUTOSTART_ENV = "WORKBENCH_HUB_AUTOSTART"
# HB-D11 exceptions (SPEC "CLI 계약"); ``--help``/``--version``/argparse errors end before.
# ``hub start`` starts the Hub itself; future ``hub`` commands are listed by name.
AUTOSTART_EXEMPT_COMMANDS = frozenset({"setup", "update", "doctor", "_backend", "_hub"})
AUTOSTART_EXEMPT_HUB_ACTIONS = frozenset({"start", "status", "stop", "enable", "disable", "passwd", "tls"})


def autostart_wanted(args: argparse.Namespace, environment=None) -> bool:
    environment = os.environ if environment is None else environment
    if environment.get(AUTOSTART_ENV) == "0":
        return False
    command = getattr(args, "command", None)
    if command in AUTOSTART_EXEMPT_COMMANDS:
        return False
    return not (command == "hub" and getattr(args, "hub_action", None) in AUTOSTART_EXEMPT_HUB_ACTIONS)


def autostart(args: argparse.Namespace) -> None:
    """Start the Hub before an auto-start command's handler; never stop the command.

    A failure is one stderr line; the managed-tree hold is shown only on a TTY.
    """
    try:  # the command must run whatever happens to the Hub or to this report
        if not autostart_wanted(args):
            return
        from .process import ensure_hub
        try:
            result = ensure_hub()
        except Exception as exc:
            reason = type(exc).__name__
        else:
            if result.ok:
                return
            reason = result.reason
        if reason in ("managed_tree", "proc_unreadable"):
            if sys.stderr.isatty():
                _report(MANAGED_TREE_TEXT if reason == "managed_tree" else PROC_UNREADABLE_AUTOSTART_TEXT)
        elif reason == "update_in_progress":
            _report("[workbench] 업데이트가 진행 중이라 Hub를 자동으로 시작하지 않았습니다.")
        else:
            _report(f"[workbench] Hub를 자동으로 시작하지 못했습니다({reason}); 명령은 계속합니다. "
                    f"log: {hub_layout().log}")
    except Exception:
        pass


def _report(line: str) -> None:
    """One unbuffered stderr line: a broken stderr leaves nothing buffered to fail at exit."""
    try:
        sys.stderr.flush()
        fd = sys.stderr.fileno()
    except (OSError, ValueError, AttributeError):
        print(line, file=sys.stderr)
        return
    try:
        os.write(fd, (line + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def cmd_hub_start(args: argparse.Namespace) -> int:
    """Spawn the Hub when none runs (regardless of ``WORKBENCH_HUB_AUTOSTART``)."""
    from .process import ensure_hub
    result = ensure_hub()
    if not result.ok:
        if result.reason in ("managed_tree", "proc_unreadable"):
            print(MANAGED_TREE_TEXT if result.reason == "managed_tree" else PROC_UNREADABLE_START_TEXT,
                  file=sys.stderr)
            return EXIT_REQUIREMENT
        print(_start_failure_text(result.reason), file=sys.stderr)
        return EXIT_FAILURE
    welcome = result.identity.welcome or {}
    lead = "Hub를 시작했습니다" if result.reason == "started" else "Hub 실행 중"
    print(f"{lead}: pid {welcome.get('pid')}, version {_version_text(welcome.get('version'))}")
    return EXIT_OK


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
