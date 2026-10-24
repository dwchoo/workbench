"""systemd ``--user`` unit for ``wb hub enable|disable`` (HB-10, SPEC "부팅 자동 시작").

The unit is written only by ``enable``. This module never changes linger and never
starts or stops a Hub; ``systemctl``/``loginctl`` are looked up on ``PATH`` at call time.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Mapping, Sequence

UNIT_NAME = "omp-workbench-hub.service"
COMMAND_TIMEOUT = 20.0
STATUS_TIMEOUT = 3.0  # `wb hub status` must stay snappy when systemd is slow
HUB_REFUSED_EXIT = 3  # main_hub: managed_tree / proc_unreadable
UNAVAILABLE_TEXT = "systemd 사용자 서비스 관리자를 쓸 수 없습니다. 설정은 바뀌지 않았습니다."

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "%": "%%", "$": "$$"}


def quote_argument(argument: str) -> str:
    """One systemd command-line word: double quoted, ``\\`` and ``"`` escaped, ``%``/``$`` doubled."""
    pieces = []
    for char in argument:
        if char in _ESCAPES:
            pieces.append(_ESCAPES[char])
        elif ord(char) < 32 or ord(char) == 127:
            pieces.append(f"\\x{ord(char):02x}")
        else:
            pieces.append(char)
    return '"' + "".join(pieces) + '"'


def hub_argv() -> list[str]:
    """The Hub command line systemd runs: the spawn bootstrap with ``_hub --foreground --mode systemd``."""
    from workbench.backend.cli import _bootstrap_source
    return [sys.executable, "-I", "-c", _bootstrap_source(), "_hub", "--foreground", "--mode", "systemd"]


def render_unit(argv: Sequence[str]) -> str:
    exec_start = " ".join(quote_argument(item) for item in argv)
    return ("[Unit]\n"
            "Description=OMP Workbench Hub\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_start}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            # exit 3 = permanent refusal (WB-managed tree / unreadable /proc): never restart-loop on it
            f"RestartPreventExitStatus={HUB_REFUSED_EXIT}\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n")


def unit_path(environment: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environment is None else environment
    config = environment.get("XDG_CONFIG_HOME")
    base = Path(config) if config and os.path.isabs(config) else Path(environment.get("HOME") or Path.home()) / ".config"
    return base / "systemd" / "user" / UNIT_NAME


@dataclass(frozen=True)
class Outcome:
    """``status``: ok | unavailable (exit 2) | failed (exit 1) | already_off (disable only)."""
    status: str
    detail: str = ""


def _run(tool: str, args: Sequence[str], timeout: float = COMMAND_TIMEOUT) -> tuple[int, str, str]:
    """(returncode, stdout, stderr); an unrunnable tool is rc 127 with the reason as stderr."""
    path = shutil.which(tool)
    if path is None:
        return 127, "", f"{tool}: command not found"
    try:
        done = subprocess.run([path, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{tool}: {type(exc).__name__}: {exc}"
    return done.returncode, done.stdout, done.stderr


def _systemctl(*args: str, timeout: float = COMMAND_TIMEOUT) -> tuple[int, str, str]:
    return _run("systemctl", ["--user", *args], timeout)


def _failure(step: str, result: tuple[int, str, str]) -> Outcome:
    message = (result[2] or result[1]).strip()
    return Outcome("failed", f"systemctl --user {step} 실패 (exit {result[0]}): {message}")


# What the unit path held before: nothing, a regular file's bytes, or a symlink's target.
Previous = tuple[str, "bytes | str"] | None


def _read_previous(path: Path) -> Previous:
    """Raises OSError (not FileNotFoundError) when the path is unreadable or not a file/symlink."""
    try:
        if path.is_symlink():
            return ("link", os.readlink(path))
        return ("file", path.read_bytes())
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _restore(path: Path, previous: Previous) -> str:
    """Put the unit back (file bytes, or the symlink itself); returns a note when even that failed."""
    try:
        if previous is None:
            path.unlink(missing_ok=True)
        elif previous[0] == "link":
            temp = path.with_name(f".{path.name}.{os.getpid()}.lnk")
            temp.unlink(missing_ok=True)
            os.symlink(previous[1], temp)
            os.replace(temp, path)
        else:
            _write_atomic(path, previous[1])
    except OSError as exc:
        return f" (unit 복원 실패: {exc})"
    return ""


def enable(environment: Mapping[str, str] | None = None) -> Outcome:
    if shutil.which("systemctl") is None or _systemctl("show-environment")[0] != 0:
        return Outcome("unavailable", UNAVAILABLE_TEXT)
    path = unit_path(environment)
    try:
        previous = _read_previous(path)
    except OSError as exc:
        return Outcome("failed", f"기존 unit을 읽지 못해 바꾸지 않았습니다: {path}: {exc}")
    try:
        _write_atomic(path, render_unit(hub_argv()).encode("utf-8"))
    except OSError as exc:
        return Outcome("failed", f"unit 파일을 쓰지 못했습니다: {path}: {exc}")
    reload_result = _systemctl("daemon-reload")
    if reload_result[0] != 0:
        return Outcome("failed", _failure("daemon-reload", reload_result).detail + _restore(path, previous))
    enable_result = _systemctl("enable", UNIT_NAME)
    if enable_result[0] != 0:
        note = _restore(path, previous)
        _systemctl("daemon-reload")  # best effort: drop the reloaded copy of the removed unit
        return Outcome("failed", _failure(f"enable {UNIT_NAME}", enable_result).detail + note)
    return Outcome("ok", str(path))


def disable(environment: Mapping[str, str] | None = None) -> Outcome:
    path = unit_path(environment)
    if not os.path.lexists(path):
        return Outcome("already_off", str(path))
    if shutil.which("systemctl") is None:
        return Outcome("unavailable", "systemctl을 찾을 수 없어 unit을 그대로 두었습니다.")
    disable_result = _systemctl("disable", UNIT_NAME)
    if disable_result[0] != 0:
        return _failure(f"disable {UNIT_NAME}", disable_result)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return Outcome("failed", f"unit 파일을 지우지 못했습니다: {path}: {exc}")
    reload_result = _systemctl("daemon-reload")
    if reload_result[0] != 0:
        return _failure("daemon-reload", reload_result)
    return Outcome("ok", str(path))


def boot_autostart_state(environment: Mapping[str, str] | None = None) -> str:
    """``enabled``/``disabled`` from ``systemctl --user is-enabled``; ``unknown`` when it cannot say."""
    if shutil.which("systemctl") is None:
        return "unknown"
    code, out, _err = _systemctl("is-enabled", UNIT_NAME, timeout=STATUS_TIMEOUT)
    word = out.strip().splitlines()[0] if out.strip() else ""
    if word in ("enabled", "disabled"):
        return word
    # systemctl exits non-zero with an empty stdout ("Failed to get unit file state ... not found")
    # for a unit that does not exist. Nothing can be enabled without our unit file, so that is
    # "disabled", not "unknown". rc 127 (not runnable / timed out) stays unknown: it said nothing.
    if code not in (0, 127) and not word and not os.path.lexists(unit_path(environment)):
        return "disabled"
    return "unknown"


def linger_state(uid: int | None = None) -> str:
    """``yes``/``no`` from ``loginctl show-user <uid> -p Linger``; ``unknown`` otherwise."""
    code, out, _err = _run("loginctl", ["show-user", str(os.geteuid() if uid is None else uid), "-p", "Linger"])
    value = out.strip().partition("=")[2].strip() if code == 0 else ""
    return value if value in ("yes", "no") else "unknown"
