"""``wb hub status`` and ``wb hub stop`` handlers (other ``wb hub`` commands come later)."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
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
