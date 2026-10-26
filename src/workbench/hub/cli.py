"""``wb hub`` handlers (``list`` included), ``wb init``, ``wb --version`` text and the systemd
enable/disable commands."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time

from workbench.backend.paths import DataDirError

from . import registry
from .client import HubClient
from .paths import hub_layout
from .process import HubNotRunning, HubUnknown, HubUnresponsive, installed_version

EXIT_OK, EXIT_FAILURE, EXIT_REQUIREMENT, EXIT_NOT_RUNNING = 0, 1, 2, 3
NOT_RUNNING_TEXT = "Hub 실행 중 아님"


def _version_text(version: object) -> str:
    if not isinstance(version, dict):
        return "unknown"
    release, commit, kind = version.get("release"), version.get("commit"), version.get("kind")
    detail = commit[:12] if kind == "git" and isinstance(commit, str) else kind
    return f"{release} ({detail})"


def version_line(version: dict | None = None) -> str:
    """``wb <release> (<commit 12>|editable|unknown)`` (SPEC "CLI 계약"); same source as the Hub record."""
    version = installed_version() if version is None else version
    kind, commit = version.get("kind"), version.get("commit")
    detail = commit[:12] if kind == "git" and isinstance(commit, str) and commit else (
        "editable" if kind == "editable" else "unknown")
    return f"wb {version.get('release')} ({detail})"


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
    from .service_unit import OUTDATED_UNIT_TEXT, boot_autostart_state, unit_outdated
    boot = boot_autostart_state()
    outdated = unit_outdated()
    if args.json:
        _print_json({"state": "running", **status, "version_drift": drift, "boot_autostart": boot,
                     "unit_outdated": outdated})
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
    print(f"  부팅 자동 시작: {boot}")
    if outdated:
        print(f"  {OUTDATED_UNIT_TEXT}")
    for line in web_status_lines(status["web"] or {}):
        print(f"  {line}")
    board = status["board_agent"]
    if board is None:
        print("  board_agent: 없음")
    else:
        print("  board_agent: " + board_text(board, (board.get("lease") or {}).get("state")))
        for line in board_failure_lines(board):
            print(line)
    return EXIT_OK


PASSWORD_NOT_SET_HINT = "'wb hub passwd'로 비밀번호를 정하세요"


def web_disabled_hint(path: object) -> str:
    """How to turn the web on (2026-10-26 user decision: off until ``hub.toml`` enables it)."""
    return (f"웹은 꺼져 있습니다. {path}에서 '[web] enabled = true'로 설정하고 "
            "'wb hub stop' 뒤 'wb hub start'로 Hub를 재시작하세요.")


def web_status_lines(web: dict) -> list[str]:
    """``web: listening <url>`` (+ the plain-HTTP warning), ``web: disabled — <hint>`` or
    ``web: not_listening (<reason>)``."""
    from .config import INSECURE_WARNING_TEXT
    state, detail = web.get("state"), web.get("detail")
    if state == "listening":
        lines = [f"web: listening {web.get('url')}"]
        if web.get("insecure_warning"):
            lines.append(f"경고: {INSECURE_WARNING_TEXT}")
        for warning in web.get("warnings") or ():
            kind, _, value = str(warning).partition(":")
            if kind == "tls_key_mode":
                lines.append(f"경고: TLS key 파일 권한이 {value}입니다. 'chmod 600 <key>'로 본인만 읽게 하세요.")
            else:
                lines.append(f"경고: {warning}")
        return lines
    if state == "disabled":
        from .config import config_path
        return [f"web: disabled — {web_disabled_hint(web.get('config') or config_path())}"]
    reason = f"{state}:{detail}" if detail else f"{state}"
    line = f"web: not_listening ({reason})"
    if state == "password_not_set" and not detail:
        line += f" — {PASSWORD_NOT_SET_HINT}"
    return [line]


LIST_TIMEOUT = 30.0  # rows are observed in parallel batches, each bounded by status.OBSERVE_TIMEOUT


def cmd_hub_list(args: argparse.Namespace) -> int:
    """Hub ``list`` with the TUI's columns (``--json``: the hub_v1 result as is)."""
    layout = hub_layout()
    try:
        with HubClient(layout, timeout=LIST_TIMEOUT) as client:
            reply = client.request("list")
    except HubNotRunning:
        from .process import managed_ancestor
        if managed_ancestor(os.getpid()) is not None:
            print(MANAGED_TREE_TEXT, file=sys.stderr)
            return EXIT_REQUIREMENT
        print(f"{NOT_RUNNING_TEXT}: Hub를 자동 시작하지 못해 목록을 볼 수 없습니다. 'wb hub start'를 실행하세요.",
              file=sys.stderr)
        return EXIT_FAILURE
    except (HubUnknown, HubUnresponsive) as exc:
        print(f"Hub에 묻지 못했습니다: {exc.reason} (lock {layout.lock})", file=sys.stderr)
        return EXIT_FAILURE
    if not reply.get("ok"):
        print(f"Hub list 실패: {reply.get('reason')}: {reply.get('detail')}", file=sys.stderr)
        return EXIT_FAILURE
    result = {key: reply.get(key) for key in ("observed_at", "registry", "rows")}
    registry_state = result["registry"] or {}
    if args.json:
        _print_json(result)
    else:
        from .presentation import render_table
        print(render_table(result["rows"] or [], time.time()))
    if registry_state.get("state") != "ok":
        print(f"Hub registry가 손상되어 목록을 읽지 못했습니다: {registry_state.get('path')}. "
              "파일을 고치거나 지운 뒤 다시 실행하세요.", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


REVIEW_MODE_TIMEOUT = 15.0


def cmd_hub_review_mode(args: argparse.Namespace) -> int:
    """``wb hub review-mode <PATH> [auto|manual]`` (D-32): the project's review mode through the Hub (its
    ``review_mode`` op stores it in ``<root>/.wb/inbox.sqlite3``); without a value, the current one."""
    root = os.path.abspath(args.path)
    fields = {"root": root} if args.mode is None else {"root": root, "set": args.mode}
    layout = hub_layout()
    try:
        with HubClient(layout, timeout=REVIEW_MODE_TIMEOUT) as client:
            reply = client.request("review_mode", **fields)
    except HubNotRunning:
        print(f"{NOT_RUNNING_TEXT}: 검토 방식을 읽거나 바꾸려면 'wb hub start'로 Hub를 시작하세요.", file=sys.stderr)
        return EXIT_FAILURE
    except (HubUnknown, HubUnresponsive) as exc:
        print(f"Hub에 묻지 못했습니다: {exc.reason} (lock {layout.lock})", file=sys.stderr)
        return EXIT_FAILURE
    if not reply.get("ok"):
        if reply.get("reason") == "not_found":
            print(f"Hub에 등록되지 않은 WB 프로젝트입니다: {root} ('wb hub add {root}'로 등록하세요)", file=sys.stderr)
        else:
            print(f"검토 방식 요청 실패: {reply.get('reason')}: {reply.get('detail')}", file=sys.stderr)
        return EXIT_FAILURE
    print(f"검토 방식: {reply.get('review_mode')}")
    return EXIT_OK


PASSWD_STDIN_LIMIT = 4 * 1024 + 2  # 1024 characters of up to 4 UTF-8 bytes, plus the line end
PASSWD_UNCHANGED = "비밀번호를 바꾸지 않았습니다."


def _read_stdin_password() -> str | None:
    """The first stdin line without its line end; ``None`` when it is not UTF-8."""
    line = sys.stdin.buffer.readline(PASSWD_STDIN_LIMIT)
    if line.endswith(b"\n"):
        line = line[:-1]
        if line.endswith(b"\r"):
            line = line[:-1]
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        return None


class _NoTerminal(Exception):
    pass


NON_ASCII_PASSWORD_WARNING = ("웹 브라우저의 비밀번호 칸은 한글 입력(IME)을 끄는 경우가 많아 "
                              "로그인하지 못할 수 있습니다.")


def _read_tty_password() -> tuple[str, str]:
    """Two ``getpass`` entries on the controlling terminal; never falls back to echoing stdin."""
    import getpass
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            first = getpass.getpass("새 비밀번호: ")
            second = getpass.getpass("새 비밀번호 확인: ")
        except getpass.GetPassWarning as exc:
            raise _NoTerminal() from exc
    return first, second


def cmd_hub_passwd(args: argparse.Namespace) -> int:
    """``wb hub passwd [--stdin]``: write ``auth.json`` (generation + 1) without starting or asking the Hub.

    The password comes only from the terminal (twice) or the first stdin line, never argv or env.
    The Hub records the ``password_changed`` audit row when it next sees the new generation.
    """
    from .auth import AuthInvalid, PASSWORD_MAX, PASSWORD_MIN, PasswordRejected, PasswordStore
    if args.stdin:
        if sys.stdin.isatty():
            return _fail("'--stdin'은 파이프·파일 입력용입니다. terminal에서는 'wb hub passwd'만 실행하세요. "
                         + PASSWD_UNCHANGED, EXIT_REQUIREMENT)
        password = _read_stdin_password()
        if password is None:
            return _fail(f"stdin이 UTF-8이 아닙니다. {PASSWD_UNCHANGED}")
        if not password.isascii():  # nobody to ask: say it and go on
            print(NON_ASCII_PASSWORD_WARNING, file=sys.stderr)
    else:
        if not sys.stdin.isatty():
            return _fail("비밀번호 입력에는 terminal이 필요합니다. 스크립트에서는 'wb hub passwd --stdin'을 쓰세요. "
                         + PASSWD_UNCHANGED, EXIT_REQUIREMENT)
        try:
            password, again = _read_tty_password()
        except _NoTerminal:
            return _fail("비밀번호 입력에는 terminal이 필요합니다. 스크립트에서는 'wb hub passwd --stdin'을 쓰세요. "
                         + PASSWD_UNCHANGED, EXIT_REQUIREMENT)
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return _fail(f"취소했습니다. {PASSWD_UNCHANGED}")
        if password != again:
            return _fail(f"두 입력이 같지 않습니다. {PASSWD_UNCHANGED}")
        if not password.isascii():
            try:
                answer = input(f"{NON_ASCII_PASSWORD_WARNING} 계속할까요? [y/N]: ")
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                return _fail(f"취소했습니다. {PASSWD_UNCHANGED}")
    try:
        PasswordStore(hub_layout().state_dir).set_password(password)
    except PasswordRejected:
        return _fail(f"비밀번호는 {PASSWORD_MIN}자 이상 {PASSWORD_MAX}자 이하여야 합니다. {PASSWD_UNCHANGED}")
    except (AuthInvalid, DataDirError, OSError) as exc:
        return _fail(f"비밀번호 파일을 쓰지 못했습니다: {exc}. {PASSWD_UNCHANGED}")
    print("비밀번호를 정했습니다. 기존 웹 로그인 세션은 모두 무효가 됩니다.")
    from .config import ConfigInvalid, config_path, load_config, write_template
    path = config_path()
    write_template()  # the file the hint points at (2026-10-26 user request); an existing one is left alone
    try:
        enabled = load_config(path).web.enabled
    except ConfigInvalid:
        enabled = True  # the Hub reports config_invalid itself; keep the usual line
    if enabled:
        print("Hub가 실행 중이면 재시작 없이 웹 listener가 열립니다.")
    else:
        print(web_disabled_hint(path))
    return EXIT_OK


def cmd_hub_tls(args: argparse.Namespace) -> int:
    """``wb hub tls --self-signed [--force]``: cert and key for the web listener with the host ``openssl``.

    Reads ``hub.toml`` (bind, ``server_names``, TLS paths) and never starts or asks the Hub: it
    keeps serving what it loaded until it is restarted.
    """
    from . import tls
    from .config import ConfigInvalid, config_path, load_config, tls_paths
    if not args.self_signed:
        print("'wb hub tls'에는 '--self-signed'가 필요합니다. 예: wb hub tls --self-signed", file=sys.stderr)
        return EXIT_REQUIREMENT
    try:
        web = load_config(config_path()).web
    except ConfigInvalid as exc:
        return _fail(f"hub.toml을 읽지 못했습니다: {exc.reason}. 고친 뒤 다시 실행하세요.")
    cert, key = tls_paths(web)
    try:
        made = tls.self_signed(web, cert, key, force=args.force)
    except tls.TlsError as exc:
        return _fail(exc.text, exc.exit_code)
    print(f"자체 서명 인증서를 만들었습니다 (유효 {tls.DAYS}일).")
    print(f"  cert: {made.cert}")
    print(f"  key: {made.key} (본인만 읽기)")
    print(f"  SAN: {', '.join(made.san)}")
    print(f"  SHA-256 지문: {made.fingerprint}")
    print("브라우저에서 처음 열 때 위 지문과 같은지 확인하세요.")
    print("Hub를 다시 시작하면 HTTPS로 열립니다.")
    return EXIT_OK


def cmd_hub_enable(args: argparse.Namespace) -> int:
    from . import service_unit
    outcome = service_unit.enable()
    if outcome.status == "unavailable":
        print(outcome.detail, file=sys.stderr)
        return EXIT_REQUIREMENT
    if outcome.status != "ok":
        print(outcome.detail, file=sys.stderr)
        return EXIT_FAILURE
    print(f"부팅 자동 시작을 켰습니다: {outcome.detail}")
    print("지금 실행 중인 Hub는 바꾸지 않았습니다.")
    linger = service_unit.linger_state()
    print(f"linger: {linger}")
    if linger == "yes":
        print("linger가 켜져 있어 부팅 직후 사용자 서비스로 Hub가 시작됩니다.")
    else:
        print("linger를 켜지 않으면 로그인 뒤에 시작됩니다.")
        print("로그인 전(부팅 직후)에 시작하려면 'loginctl enable-linger'가 필요합니다. 이 명령은 그것을 바꾸지 않습니다.")
    return EXIT_OK


def cmd_hub_disable(args: argparse.Namespace) -> int:
    from . import service_unit
    outcome = service_unit.disable()
    if outcome.status == "already_off":
        print("이미 꺼져 있습니다")
        return EXIT_OK
    if outcome.status != "ok":
        print(outcome.detail, file=sys.stderr)
        return EXIT_REQUIREMENT if outcome.status == "unavailable" else EXIT_FAILURE
    print(f"부팅 자동 시작을 껐습니다: {outcome.detail} 삭제")
    print("실행 중인 Hub는 멈추지 않았습니다. 멈추려면 'wb hub stop'을 실행하세요.")
    return EXIT_OK


LEASE_TEXT = {"connected": "연결됨", "none": "연결 없음"}


def board_text(channels: dict, lease: str | None) -> str:
    """``agent running(pid N) · shell running(pid M) · 연결 없음`` (SPEC "Board agent 세트" 상태)."""
    parts = []
    for name in ("agent", "shell"):
        channel = channels.get(name) or {}
        state = channel.get("state") or "unknown"
        pid = channel.get("pid")
        parts.append(f"{name} {state}" + (f"(pid {pid})" if pid is not None and state in ("starting", "running")
                                          else ""))
    if lease is not None:
        parts.append(LEASE_TEXT.get(lease, str(lease)))
    return " · ".join(parts)


def board_failure_lines(board: dict) -> list[str]:
    """``    <channel>: <message>`` for each channel that failed with an explanation (directory, fix)."""
    lines = []
    for name in ("agent", "shell"):
        channel = board.get(name)
        message = channel.get("message") if isinstance(channel, dict) else None
        if isinstance(message, str) and message:
            lines.append(f"    {name}: {message}")
    return lines


def active_text(item) -> str:
    """One ``stop_request.active`` entry for the stop question."""
    if isinstance(item, dict) and item.get("kind") == "board_agent_set":
        return "Board agent 세트: " + board_text(item.get("channels") or {}, item.get("lease"))
    return str(item)


def _ask_active(args: argparse.Namespace):
    def ask(active: list) -> bool:
        print("Hub를 멈추면 다음도 끝납니다:")
        for item in active:
            print(f"  - {active_text(item)}")
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
# ``wb logs <kind>``: diagnostic log reads like ``doctor``; they start no Hub and create nothing under HOME.
AUTOSTART_EXEMPT_LOGS_KINDS = frozenset({"important", "raw", "raw-status", "raw-rotation", "summary-compat"})


def autostart_wanted(args: argparse.Namespace, environment=None) -> bool:
    environment = os.environ if environment is None else environment
    if environment.get(AUTOSTART_ENV) == "0":
        return False
    command = getattr(args, "command", None)
    if command in AUTOSTART_EXEMPT_COMMANDS:
        return False
    if command == "logs" and getattr(args, "logs_kind", None) in AUTOSTART_EXEMPT_LOGS_KINDS:
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
