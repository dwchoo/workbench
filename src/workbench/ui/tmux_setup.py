"""Make tmux's mouse / allow-passthrough usable before the product UI starts (inside tmux only)."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Callable, Mapping, Sequence

from wcwidth import wcswidth

# option -> values that already count as set (allow-passthrough "all" also passes through from invisible panes)
MAX_NOTICE_COLUMNS = 79  # footer width of the smallest supported terminal
BEGIN, END = "# >>> workbench tmux >>>", "# <<< workbench tmux <<<"
TARGETS: dict[str, frozenset[str]] = {
    "mouse": frozenset({"on"}),
    "allow-passthrough": frozenset({"on", "all"}),
}


def config_path(environ: Mapping[str, str], home: Path) -> Path:
    """The tmux config to extend: existing ~/.tmux.conf, else existing XDG tmux.conf, else a new ~/.tmux.conf."""
    classic = home / ".tmux.conf"
    xdg = Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "tmux" / "tmux.conf"
    if not classic.exists() and xdg.exists():
        return xdg
    return classic


def _shown(path: Path, home: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _wants_file(ask: Callable[[str], str], missing: list[str], path: Path, home: Path) -> bool:
    prompt = (f"tmux 설정이 없습니다: {', '.join(missing)}\n"
              f"{_shown(path, home)} 에 추가할까요? (아니오면 이번 tmux 서버에만 적용) [y/N] ")
    try:
        return ask(prompt).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _has_block(path: Path) -> bool:
    try:
        return BEGIN in path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False


def _append_block(path: Path, options: list[str]) -> None:
    """Append-only: existing content is never rewritten."""
    existing = path.read_bytes() if path.exists() else b""
    lead = b"" if not existing else (b"\n" if existing.endswith(b"\n") else b"\n\n")
    lines = [BEGIN, *(f"set -g {option} on" for option in options), END]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(lead + ("\n".join(lines) + "\n").encode())


@dataclass(frozen=True)
class TmuxSetupResult:
    applied: tuple[str, ...] = ()
    written_path: Path | None = None
    notice: str = ""


def _missing_options(run: Callable[[Sequence[str]], tuple[int, str]]) -> list[str] | None:
    """Target options the running server lacks; None when tmux cannot be read (the caller says so, or stays silent)."""
    missing = []
    for option, accepted in TARGETS.items():
        code, out = run(["show", "-gv", option])
        if code != 0:
            if option == "mouse":
                return None
            continue  # allow-passthrough unknown to this tmux (< 3.3): never applied, never written
        if out.strip() not in accepted:
            missing.append(option)
    return missing


def _fit(parts: list[str], where: str, name: str) -> str:
    """The notice with the config path when it fits in 79 display columns, else with just the file name."""
    template = "".join(parts)
    notice = template.replace("{where}", where)
    return notice if wcswidth(notice) <= MAX_NOTICE_COLUMNS else template.replace("{where}", name)


def ensure_tmux_options(*, environ: Mapping[str, str], run: Callable[[Sequence[str]], tuple[int, str]],
                        interactive: bool, ask: Callable[[str], str], home: Path) -> TmuxSetupResult:
    """Make the running tmux server's ``mouse`` / ``allow-passthrough`` on. Never raises, never blocks for long.

    Acts only inside tmux. With a TTY it asks once whether to append the missing lines to the tmux config as well;
    any other answer (EOF, Ctrl-C, empty) applies them to the running server only.
    """
    if not environ.get("TMUX"):
        return TmuxSetupResult()
    try:
        missing = _missing_options(run)
    except FileNotFoundError:
        return TmuxSetupResult()  # no tmux binary: nothing to set up
    except (OSError, subprocess.SubprocessError):
        return TmuxSetupResult(notice="tmux 설정 확인 실패 — 건너뜀")
    if missing is None:
        return TmuxSetupResult(notice="tmux 설정 확인 실패 — 건너뜀")
    if not missing:
        return TmuxSetupResult()
    path, written, write_failed = config_path(environ, home), None, False
    if interactive:
        try:
            if not _has_block(path) and _wants_file(ask, missing, path, home):
                _append_block(path, missing)
                written = path
        except OSError:
            write_failed = True
    applied, failed = [], []
    for option in missing:
        try:
            code, _ = run(["set", "-g", option, "on"])
        except (OSError, subprocess.SubprocessError):
            code = 1
        (applied if code == 0 else failed).append(option)
    parts = ["tmux 설정 적용: " + ", ".join(f"{o} on" for o in applied)] if applied else []
    if failed:
        parts.append((" · 적용 실패: " if applied else "tmux 설정 적용 실패: ") + ", ".join(failed))
    if written is not None:
        parts.append(" ({where}에 추가)")
    if write_failed:
        parts.append(" · {where} 쓰기 실패")
    return TmuxSetupResult(tuple(applied), written, _fit(parts, _shown(path, home), path.name))


def tmux_runner(environ: Mapping[str, str], timeout: float = 2.0) -> Callable[[Sequence[str]], tuple[int, str]]:
    """Run ``tmux <args>`` against the server named by ``environ["TMUX"]``; the short timeout keeps startup responsive."""
    def run(args: Sequence[str]) -> tuple[int, str]:
        done = subprocess.run(["tmux", *args], env=dict(environ), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout
    return run


def setup_tmux_for_ui(stdin_fd: int = 0, stdout_fd: int = 1, environ: Mapping[str, str] | None = None) -> str:
    """The product UI's pre-start hook: returns the footer notice ("" when there is nothing to say). Never raises."""
    environ = os.environ if environ is None else environ
    try:
        result = ensure_tmux_options(environ=environ, run=tmux_runner(environ),
                                     interactive=os.isatty(stdin_fd) and os.isatty(stdout_fd), ask=input,
                                     home=Path.home())
    except Exception:  # startup must never fail on a convenience step
        return "tmux 설정 확인 실패 — 건너뜀"
    return result.notice
