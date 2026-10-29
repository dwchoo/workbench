"""Board agent OMP launch: home and ``wb-inbox`` skill copy, system key, internal REST listener, overlay, argv and
isolation check.

Task Inbox SPEC "Board agent 세트", ADR-0003, web-board-ux SPEC "Board agent 전환" (WBX-B5: REST and the ``wb-inbox``
skill instead of MCP) and the WBX-B4 probe (PROBE-BOARD-SKILL "WBX-B5가 쓸 구성").
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import functools
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping

from workbench.assets import canonical_path
from workbench.backend import launcher, omp_home
from workbench.backend.launcher import (DISPLAY_TERM, ISOLATION_PROVIDER_IDS, ModelTableError,
                                        default_isolation_overlay, load_model_table)
from workbench.backend.paths import DataDirError, ensure_private_dir, write_private_bytes

from .. import auth, rest
from ..config import BoardAgentConfig
from ..process import hub_environment

BOARD_DIR_NAME = "board-agent"
EMPTY_DIR_NAME = "empty"
OVERLAY_NAME = "omp-board.yml"
LEGACY_MCP_FILE = "mcp.json"  # ``<agent dir>/mcp.json`` of the MCP Board (before WBX-B5): removed at every start
# WBX-B5: the Board agent's only skill, copied from the installed ``skills/wb-inbox`` at every start (never ``.env``).
SKILLS_DIR_NAME = "skills"
SKILL_NAME = "wb-inbox"
SKILL_FILES = ("SKILL.md", "wb_inbox.py")
SESSIONS_DIR_NAME = "sessions"
# WBX-B12: OMP writes ``<config root>/logs/omp.<date>.<pid>.log`` and prunes that directory itself; each Board agent
# start keeps a redacted copy in ``board-agent/omp-logs`` (newest OMP_LOG_KEEP files, each at most OMP_LOG_FILE_LIMIT
# bytes: its start and its end) so a later login problem can still be diagnosed.
OMP_LOGS_DIR_NAME = "logs"
OMP_LOG_ARCHIVE_NAME = "omp-logs"
OMP_LOG_KEEP = 20
OMP_LOG_FILE_LIMIT = 1 << 20
_OMP_LOG_NAME = re.compile(r"^omp\.[0-9A-Za-z._-]+\.log$")
_LOG_CUT = b"\n[workbench: log middle omitted]\n"
_LOG_MARGIN = 64 << 10  # read beyond each kept window so a secret crossing the cut is redacted whole first
OMP_EVENTS_NAME = "board-events.log"  # one JSON line per Board agent start and stop (WBX-B12)
OMP_EVENTS_LIMIT = 256 << 10
_REDACTED = b"[redacted]"
# Redaction runs over the whole kept text; every pattern is linear (atomic key groups, a key never starts right
# after another key character, quoted values stop at the line end), so no input makes a start slow.
_KEY = rb"(?>[A-Za-z0-9_.-]+)"
_QUOTED = rb"\"(?:[^\"\\\n]|\\.)*+\"|'(?:[^'\\\n]|\\.)*+'"
_SCHEME = rb"(?i:bearer|basic|digest|token|negotiate)[ \t]+"
_SENSITIVE = ("token", "secret", "password", "passwd", "apikey", "api_key", "authorization", "cookie",
              "credential", "private_key", "session")
_HUB_KEY = re.compile(rb"wbk_[a-z2-7]{10}_[A-Za-z0-9_-]++")  # a Hub API key (the Board system key included)
_SK_KEY = re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}+")
_BEARER = re.compile(rb"(?i)(?<![A-Za-z0-9_-])(bearer\s++)[^\s\"',;]++")
_COOKIE = re.compile(rb"(?im)^([ \t]*+(?:set-)?cookie[ \t]*+:)[^\n]*+")
_QUOTED_FIELD = re.compile(rb"([\"'])(" + _KEY + rb")\1(\s*+[:=]\s*+)(" + _QUOTED + rb"|[^\s,;{}\[\]\"']++)")
_PLAIN_FIELD = re.compile(rb"(?<![A-Za-z0-9_.\"'-])(" + _KEY + rb")([ \t]*+[:=][ \t]*+)(" + _QUOTED
                          + rb"|(?:" + _SCHEME + rb")?[^\s,;&{}\[\]\"']++)")
# The Board agent's ``wb-inbox`` skill reads its Hub from these (WBX-D11): this start's system key and the internal
# listener's origin (no path; ``wb_inbox.py`` adds ``/api/v1/…``).
KEY_ENV = "WB_API_KEY"
URL_ENV = "WB_HUB_URL"
NO_PROXY_ENV = ("NO_PROXY", "no_proxy")  # urllib follows ``*_PROXY``: the loopback listener is always direct
CHECK_ROLE = "board"
DEFAULT_SHELL = "/bin/sh"
BASH_CANDIDATES = ("/bin/bash", "/usr/bin/bash")  # then ``bash`` on the Hub's PATH (WBX-D4)
REASON_LIMIT = 256  # the audit ``reason`` limit
MESSAGE_LIMIT = 1024  # the human ``message`` next to a failed channel
FALLBACK_LOCALE = "C.UTF-8"
LOCALE_KEYS = ("LC_ALL", "LC_CTYPE", "LANG")  # the C library's precedence for the character type
_UTF8_LOCALE = re.compile(r"\.utf-?8(@|$)", re.IGNORECASE)


def _log(text: str) -> None:
    """One ``hub.log`` line (the Hub's stderr), like ``board.set``'s."""
    print(f"[workbench] board: {text}", file=sys.stderr, flush=True)


class BoardLaunchError(RuntimeError):
    """The Board agent OMP cannot be started; ``reason`` is the short status detail (a stable code).
    ``message`` is the optional human explanation (directory, cause, fix) shown next to it, never in its place."""

    def __init__(self, reason: str, text: str = "", *, message: str | None = None):
        super().__init__(text or reason)
        self.reason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Which Board agent starts (WBX-D44): its registry id and name and its home directory (absolute)."""

    id: str
    name: str
    home: Path


@dataclass(frozen=True, slots=True)
class AgentLaunch:
    """One Board agent start. ``env`` carries the system key; never log or audit it."""

    argv: list[str]
    env: dict[str, str] = field(repr=False)  # holds the raw system key
    cwd: str = ""
    key_id: str = ""
    started_at: float = 0.0  # wall clock of prepare_agent (the Board events line)
    home: Path | None = None  # the agent's home (its ``omp-logs/board-events.log``)


@dataclass(frozen=True, slots=True)
class ShellLaunch:
    argv: list[str]
    env: dict[str, str]
    cwd: str


def board_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / BOARD_DIR_NAME


def _remove(path: Path) -> None:
    """``path`` gone, whatever it is; a symlink is removed itself, never followed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def install_board_skill(home_dir: Path | str) -> Path:
    """Rebuild ``<agent home>/skills/wb-inbox/`` (0700) from the installed skill's :data:`SKILL_FILES` (0600, nothing
    else); returns the skills directory."""
    skills = ensure_private_dir(Path(home_dir)) / SKILLS_DIR_NAME
    _remove(skills)
    folder = ensure_private_dir(ensure_private_dir(skills) / SKILL_NAME)
    for name in SKILL_FILES:
        write_private_bytes(folder / name, canonical_path(f"skills/{SKILL_NAME}/{name}").read_bytes())
    return skills


def prepare_board_home(home_dir: Path | str, environment: Mapping[str, str]) -> omp_home.OmpHome:
    """``<agent home>/omp-root`` with ``agent.db`` linked to the user's store (``omp_home`` rules; no copy, C-D64).
    The agent home is ``<hub state>/board-agent`` (the migrated ``agent-1``) or ``<hub state>/board-agents/<id>``.

    Every isolation provider stays off (``native`` included: no MCP file is read); the only skill directory is the fresh
    ``wb-inbox`` copy (:func:`install_board_skill`); no Workbench subagent definitions (an empty private directory).
    """
    root = ensure_private_dir(Path(home_dir))
    empty = ensure_private_dir(root / EMPTY_DIR_NAME)
    skills = install_board_skill(root)
    return omp_home.prepare_omp_home(root, environment, skills_dir=skills, provider_ids=ISOLATION_PROVIDER_IDS,
                                     agents_dir=empty)


def _sensitive(key: bytes) -> bool:
    name = key.decode("ascii", "replace").lower().replace("-", "_")
    return any(word in name or word in name.replace("_", "") for word in _SENSITIVE)


def _field(match: re.Match, key_group: int, value_group: int) -> bytes:
    if not _sensitive(match.group(key_group)):
        return match.group(0)
    value = match.group(value_group)
    quote = value[:1] if value[:1] in (b'"', b"'") else b""
    start = match.start(value_group) - match.start(0)
    return match.group(0)[:start] + quote + _REDACTED + quote


def redact_log(data: bytes) -> bytes:
    """``data`` without Hub keys, ``sk-`` keys, bearer values, cookies, or values of fields whose name says token,
    secret, password, API key, authorization, cookie, credential or session (JSON, dict, ``key: value``, ``key=value``,
    header forms). Linear in ``len(data)``."""
    data = _HUB_KEY.sub(_REDACTED, data)
    data = _SK_KEY.sub(_REDACTED, data)
    data = _COOKIE.sub(lambda match: match.group(1) + b" " + _REDACTED, data)
    data = _QUOTED_FIELD.sub(lambda match: _field(match, 2, 4), data)
    data = _PLAIN_FIELD.sub(lambda match: _field(match, 1, 3), data)
    return _BEARER.sub(lambda match: match.group(1) + _REDACTED, data)


def _read_redacted(fd: int, size: int) -> bytes:
    """The redacted file, or its redacted first and last ``OMP_LOG_FILE_LIMIT/2`` bytes cut at line ends; each window is
    read and redacted with a margin first, so a secret crossing a cut is redacted whole or dropped with its line."""
    if size <= OMP_LOG_FILE_LIMIT:
        return redact_log(os.pread(fd, OMP_LOG_FILE_LIMIT, 0))
    half = OMP_LOG_FILE_LIMIT // 2
    head = redact_log(os.pread(fd, half + _LOG_MARGIN, 0))[:half]
    if b"\n" in head:
        head = head[:head.rindex(b"\n") + 1]
    start = max(0, size - half - _LOG_MARGIN)
    tail = redact_log(os.pread(fd, size - start, start))[-half:]
    if b"\n" in tail[:-1]:
        tail = tail[tail.index(b"\n") + 1:]
    return head + _LOG_CUT.lstrip(b"\n") + tail


def _open_logs_dir(path: Path) -> int | None:
    """The OMP logs directory as an fd: a plain directory this user owns, never through a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        os.close(fd)
        return None
    return fd


def archive_omp_logs(home_dir: Path | str, home: omp_home.OmpHome) -> list[str]:
    """Copy the agent's OMP logs (regular files this user owns, never through a symlink) into ``<agent home>/omp-logs``
    (0700, files 0600, redacted, bounded) and keep the newest :data:`OMP_LOG_KEEP` there. Returns the names copied."""
    archive = ensure_private_dir(Path(home_dir) / OMP_LOG_ARCHIVE_NAME)
    source = _open_logs_dir(home.root / OMP_LOGS_DIR_NAME)
    copied = []
    if source is not None:
        try:
            names = sorted(name for name in os.listdir(source) if _OMP_LOG_NAME.match(name))
            for name in names:
                if _archive_one(source, name, archive):
                    copied.append(name)
        finally:
            os.close(source)
    entries = []
    for name in os.listdir(archive):
        if not _OMP_LOG_NAME.match(name):
            continue
        try:
            entries.append((os.lstat(archive / name).st_mtime_ns, name))
        except OSError:
            continue
    for _, name in sorted(entries, reverse=True)[OMP_LOG_KEEP:]:
        try:
            os.unlink(archive / name)
        except OSError:
            pass
    return copied


def _archive_one(source: int, name: str, archive: Path) -> bool:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=source)
    except OSError:
        return False  # a symlink, gone, unreadable
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return False
        target = archive / name
        try:
            kept = os.lstat(target)
            if kept.st_mtime_ns == info.st_mtime_ns and stat.S_ISREG(kept.st_mode):
                return False  # unchanged since the last start
        except FileNotFoundError:
            pass
        write_private_bytes(target, _read_redacted(fd, info.st_size))
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
        return True
    finally:
        os.close(fd)


def _stamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def record_board_event(home_dir: Path | str, event: Mapping[str, Any]) -> None:
    """Append one redacted JSON line to ``<agent home>/omp-logs/board-events.log`` (0600, never through a symlink);
    past :data:`OMP_EVENTS_LIMIT` bytes only the newer half of the lines is kept."""
    if not os.path.isdir(home_dir) or os.path.islink(home_dir):
        return  # a deleted agent's home is never made again by a late event line
    archive = ensure_private_dir(Path(home_dir) / OMP_LOG_ARCHIVE_NAME)
    path = archive / OMP_EVENTS_NAME
    line = redact_log(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).replace(b"\n", b" ") + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        if info.st_size + len(line) <= OMP_EVENTS_LIMIT:
            os.write(fd, line)
            return
        kept = os.pread(fd, info.st_size, 0)[-(OMP_EVENTS_LIMIT // 2):]
    finally:
        os.close(fd)
    if b"\n" in kept:
        kept = kept[kept.index(b"\n") + 1:]
    write_private_bytes(path, kept + line)


def board_overlay(skills_dir: Path | str) -> dict[str, Any]:
    """The second ``--config`` after ``omp-isolation.yml`` (a later array replaces the earlier one), as the WBX-B4
    probe ran it: every isolation provider off, the ``wb-inbox`` copy as the only skill, no project MCP files,
    OMP's ``seance`` agent off as for the manager/worker, memory back on (D-25)."""
    return {"disabledProviders": list(ISOLATION_PROVIDER_IDS), "mcp": {"enableProjectConfig": False},
            "skills": {"customDirectories": [str(skills_dir)], "includeSkills": [SKILL_NAME], "ignoredSkills": []},
            "task": {"disabledAgents": ["seance"]},
            "memory": {"backend": "local"}, "memories": {"enabled": True}}


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


def write_board_files(home_dir: Path | str, home: omp_home.OmpHome) -> Path:
    """Rewrite the overlay (0600) and remove an older version's ``mcp.json`` at every start; returns the overlay."""
    overlay = Path(home_dir) / OVERLAY_NAME
    write_private_bytes(overlay, _json_bytes(board_overlay(Path(home_dir) / SKILLS_DIR_NAME)))
    _remove(home.agent_dir / LEGACY_MCP_FILE)
    return overlay


def default_role_prompt() -> Path:
    return canonical_path("omp_bridge/board/ROLE.md")


def role_prompt(config: BoardAgentConfig) -> str:
    """``omp_bridge/board/ROLE.md``, then ``board_agent.role_prompt_file`` when set (appended, never replacing)."""
    try:
        text = default_role_prompt().read_text(encoding="utf-8")
    except OSError as exc:
        raise BoardLaunchError("role_prompt_missing", str(exc)) from exc
    if not config.role_prompt_file:
        return text
    try:
        extra = Path(config.role_prompt_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BoardLaunchError("role_prompt_file_unreadable", str(exc)) from exc
    return text.rstrip("\n") + "\n\n" + extra


def board_model(config: BoardAgentConfig, models_file: Path | str | None = None) -> str:
    """``board_agent.model``, or the manager's ``default`` selector of ``omp-models.yml`` (Q-07)."""
    if config.model:
        return config.model
    try:
        return load_model_table(models_file)["manager"]["models"]["default"]
    except ModelTableError as exc:
        raise BoardLaunchError("model_table_invalid", str(exc)) from exc


def previous_session(home: omp_home.OmpHome) -> bool:
    """Whether the Board home keeps an OMP session to ``--continue`` (``<agent dir>/sessions/<cwd slug>/``)."""
    sessions = home.agent_dir / SESSIONS_DIR_NAME
    try:
        return any(path.is_file() for path in sessions.rglob("*.jsonl"))
    except OSError:
        return False


def board_argv(omp: str, overlay: Path | str, prompt: str, *, resume: bool, model: str | None) -> list[str]:
    """``omp --config <isolation> --config <board overlay> --no-extensions --append-system-prompt <role>
    --no-title --allow-home [--continue] [--model <selector>]``; no Workbench bridge extension."""
    argv = [omp, "--config", str(default_isolation_overlay()), "--config", str(overlay), "--no-extensions",
            "--append-system-prompt", prompt, "--no-title", "--allow-home"]
    if resume:
        argv.append("--continue")
    if model:
        argv += ["--model", model]
    return argv


def resolve_omp(environment: Mapping[str, str]) -> str:
    from workbench.backend.setup import SetupError, resolve_runtime_selection
    try:
        selection = resolve_runtime_selection(None, environment)
    except SetupError as exc:
        # Same facts `wb` prints (launcher.build_plan): the selected path, the cause and the one-line fix.
        message = f"OMP selection failed (selected {exc.path or 'unknown'}): {exc.explain()}."
        message += f" Fix: {exc.fix}, then run wb setup." if exc.fix else " Resolve this, then run wb setup."
        raise BoardLaunchError("omp_unavailable", str(exc), message=message[:MESSAGE_LIMIT]) from exc
    path = str(selection.path)
    if not os.access(path, os.X_OK):
        raise BoardLaunchError("omp_unavailable", f"not executable: {path}")
    return path


def isolation_failure(result: Mapping[str, Any]) -> str | None:
    """None when the checked OMP showed no leak (any MCP tool is one) and the ``wb-inbox`` skill; otherwise the
    reason: the leaks named, ``skill_missing:wb-inbox``, or why the check itself failed (SPEC "격리 검사")."""
    if result.get("error") or result.get("observed") is None:
        return f"check_failed:{result.get('error') or result.get('state')}"[:REASON_LIMIT]
    leaks = list(result.get("leaks") or [])
    if leaks:
        return ("leak:" + ",".join(leaks))[:REASON_LIMIT]
    if SKILL_NAME not in (result["observed"].get("skills") or []):
        return f"skill_missing:{SKILL_NAME}"
    return None


@functools.lru_cache(maxsize=1)
def installed_locales() -> frozenset[str] | None:
    """``locale -a`` once per Hub process, names normalized (:func:`_locale_key`); None when it cannot run."""
    try:
        done = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return frozenset(_locale_key(name) for name in done.stdout.split())


def _locale_key(name: str) -> str:
    """glibc's normalized codeset: ``ko_KR.UTF-8`` and ``ko_KR.utf8`` name the same locale."""
    language, dot, rest = name.partition(".")
    codeset, at, modifier = rest.partition("@")
    return language + dot + "".join(ch for ch in codeset.lower() if ch.isalnum()) + at + modifier


def _usable_utf8(name: str) -> bool:
    if not _UTF8_LOCALE.search(name):
        return False
    installed = installed_locales()
    return installed is None or _locale_key(name) in installed  # unknown: trust the name


def utf8_locale(env: dict[str, str]) -> dict[str, str]:
    """``env`` with a UTF-8 character type: the browser terminal sends and shows UTF-8 (Hangul input, readline
    erase). The user's installed UTF-8 locale stays. Otherwise only the character type changes, to
    :data:`FALLBACK_LOCALE` through ``LC_CTYPE``, so the other categories survive: a non-UTF-8 ``LC_ALL`` (which
    masked every ``LC_*``) is unset and its value moves to ``LANG``; no locale at all sets ``LANG``."""
    lc_all = env.get("LC_ALL")
    if lc_all:
        if _usable_utf8(lc_all):
            return env
        for key in [key for key in env if key.startswith("LC_")]:
            del env[key]
        env["LANG"], env["LC_CTYPE"] = lc_all, FALLBACK_LOCALE
        return env
    for key in ("LC_CTYPE", "LANG"):
        if env.get(key):
            if not _usable_utf8(env[key]):
                env["LC_CTYPE"] = FALLBACK_LOCALE
            return env
    env["LANG"] = FALLBACK_LOCALE
    return env


def _with_loopback(value: str | None) -> str:
    """A ``NO_PROXY`` list with ``127.0.0.1`` in it (the user's entries kept, in their order)."""
    hosts = [item.strip() for item in (value or "").split(",") if item.strip()]
    return ",".join(hosts if rest.INTERNAL_HOST in hosts else [*hosts, rest.INTERNAL_HOST])


def board_environment(base: Mapping[str, str], home: omp_home.OmpHome, key: str, url: str) -> dict[str, str]:
    """``hub_environment()`` with the Board home values, ``TERM``, a UTF-8 locale, the system key as ``WB_API_KEY``,
    the internal listener's origin as ``WB_HUB_URL`` and loopback in ``NO_PROXY``/``no_proxy`` (HOME stays the
    user's)."""
    env = utf8_locale(omp_home.home_environment(hub_environment(base), home.environment()))
    env["TERM"] = DISPLAY_TERM
    env[KEY_ENV] = key
    env[URL_ENV] = url
    for name in NO_PROXY_ENV:
        env[name] = _with_loopback(env.get(name))
    return env


class HomeRemovalRefused(Exception):
    """An agent home that must not be removed (outside ``<hub state>``, a symlink, another owner); nothing changed."""


def remove_board_home(state_dir: Path | str, relative: str) -> None:
    """Remove an agent's home ``<hub state>/<relative>`` (WBX-D44 "삭제") without following any symlink: every path
    component is opened ``O_NOFOLLOW`` below the Hub state directory and must be this user's directory; the tree is
    removed through directory fds (``shutil.rmtree(dir_fd=)``), so a symlink inside (``agent.db``) is unlinked, never
    its target. A missing home is already removed. :class:`HomeRemovalRefused` before anything changes; an
    ``OSError`` once some of it is gone (a partial delete)."""
    parts = Path(relative).parts
    if not parts or any(part in ("", ".", "..") or "/" in part for part in parts) or Path(relative).is_absolute():
        raise HomeRemovalRefused(f"{relative} is not a home below the Hub state directory")
    state = Path(state_dir)
    path = state.joinpath(*parts)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        current = os.open(state, flags)
    except OSError as exc:
        raise HomeRemovalRefused(f"{state} cannot be opened: {exc.strerror}") from None
    try:
        for part in parts[:-1]:
            try:
                following = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise HomeRemovalRefused(f"{path} is not below {state} through plain directories "
                                         f"({part}: {exc.strerror})") from None
            os.close(current)
            current = following
            if os.fstat(current).st_uid != os.geteuid():
                raise HomeRemovalRefused(f"{path} is in a directory another user owns")
        name = parts[-1]
        try:
            info = os.lstat(name, dir_fd=current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise HomeRemovalRefused(f"{path} is not a plain directory (a symlink is never followed)")
        if info.st_uid != os.geteuid():
            raise HomeRemovalRefused(f"{path} is owned by another user")
        shutil.rmtree(name, dir_fd=current)
    finally:
        os.close(current)


class BoardLauncher:
    """Prepares each Board agent start; owned by the Hub's ``BoardSet``.

    ``api_keys`` returns the Hub's key store (``hub.sqlite3``); ``listener`` is the Board agents' one internal REST
    listener (WBX-B5), or a callable that makes it on the first start (the key store may open only then); it is
    started on the first start and stopped by :meth:`close`. Every agent gets its own home and its own system key
    ``board-agent.<agent id>`` (WBX-D44/D58); role prompt, model, overlay rules and the isolation check are shared.
    """

    def __init__(self, state_dir: Path | str, config: BoardAgentConfig, *,
                 api_keys: Callable[[], auth.ApiKeyStore],
                 listener: rest.InternalListener | Callable[[], rest.InternalListener],
                 environment: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time,
                 check_timeout: float = launcher.ISOLATION_CHECK_TIMEOUT):
        self.state_dir = Path(state_dir)
        self.check_timeout = check_timeout
        self.config = config
        self.api_keys = api_keys
        self._listener_source = listener
        self.listener: rest.InternalListener | None = listener if isinstance(listener, rest.InternalListener) else None
        self.environment = dict(os.environ if environment is None else environment)
        self.clock = clock

    async def _blocking(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)

    def _home(self, home_dir: Path) -> omp_home.OmpHome:
        try:
            return prepare_board_home(home_dir, self.environment)
        except (omp_home.OmpHomeError, DataDirError, OSError) as exc:
            raise BoardLaunchError("home_unavailable", str(exc),
                                   message=f"Board agent home {home_dir}: {exc}"[:MESSAGE_LIMIT]) from exc

    def _keep_logs(self, home_dir: Path, home: omp_home.OmpHome) -> None:
        try:
            archive_omp_logs(home_dir, home)
        except Exception as exc:  # noqa: BLE001 - diagnostics only: never blocks a start (DataDirError, OSError, ...)
            _log(f"OMP log copy skipped: {type(exc).__name__}: {exc}")

    def _rotate(self, agent_id: str) -> auth.IssuedKey:
        try:
            return self.api_keys().rotate_system_key(agent_id, now=float(self.clock()))
        except Exception as exc:  # noqa: BLE001 - HubDbError, sqlite3, a missing store, name_taken
            raise BoardLaunchError("hub_db_unavailable", type(exc).__name__) from exc

    def drop_key(self, agent_id: str, key_id: str | None = None) -> None:
        """Blocking (executor): the agent's system key row gone once its process ended (WBX-D58); never raises."""
        try:
            self.api_keys().delete_system_key(agent_id, key_id)
        except Exception as exc:  # noqa: BLE001 - the store may be closed at Hub stop
            _log(f"system key removal skipped: {type(exc).__name__}")

    def purge_keys(self) -> bool:
        """Blocking (executor): every system key row gone (Board creation); False while the key store is closed."""
        try:
            self.api_keys().delete_system_keys()
        except Exception as exc:  # noqa: BLE001
            _log(f"system key purge deferred: {type(exc).__name__}")
            return False
        return True

    def key_name_free(self, agent_id: str) -> bool:
        """Blocking: no key (a person's from before the prefix was reserved) has this agent's key name."""
        try:
            return not self.api_keys().name_in_use(auth.system_key_name(agent_id))
        except Exception:  # noqa: BLE001 - checked again (name_taken) at the start
            return True

    async def prepare_agent(self, agent: AgentSpec) -> AgentLaunch:
        """Home and skill copy, a new system key, the internal listener, overlay and argv for one start of
        ``agent``."""
        omp = resolve_omp(self.environment)
        prompt = role_prompt(self.config)
        model = board_model(self.config)
        home = await self._blocking(self._home, agent.home)
        await self._blocking(self._keep_logs, agent.home, home)
        resume = await self._blocking(previous_session, home)
        issued = await self._blocking(self._rotate, agent.id)
        try:
            if self.listener is None:
                self.listener = self._listener_source()
            url = await self.listener.start()
        except RuntimeError as exc:  # the Hub's key store is not open (web services stopped)
            await self._blocking(self.drop_key, agent.id, issued.key_id)
            raise BoardLaunchError("hub_db_unavailable", str(exc)) from exc
        except OSError as exc:
            await self._blocking(self.drop_key, agent.id, issued.key_id)
            raise BoardLaunchError("internal_listener_failed", str(exc)) from exc
        try:
            overlay = await self._blocking(write_board_files, agent.home, home)
        except OSError as exc:
            await self._blocking(self.drop_key, agent.id, issued.key_id)
            raise BoardLaunchError("home_unavailable", str(exc)) from exc
        env = board_environment(self.environment, home, issued.raw, url)
        cwd = str(omp_home.environment_home(self.environment))
        return AgentLaunch(board_argv(omp, overlay, prompt, resume=resume, model=model), env, cwd, issued.key_id,
                           float(self.clock()), Path(agent.home))

    def shell_program(self) -> str:
        """WBX-D4: ``[board_agent] shell`` (it must be an absolute, executable path: else ``shell_unavailable``), else
        bash (:data:`BASH_CANDIDATES`, then ``bash`` on the Hub's PATH), else :data:`DEFAULT_SHELL`. ``$SHELL`` is not
        consulted."""
        if self.config.shell:
            shell = self.config.shell
            if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
                raise BoardLaunchError("shell_unavailable", shell)
            return shell
        for candidate in (*BASH_CANDIDATES, shutil.which("bash", path=self.environment.get("PATH", os.defpath))):
            if candidate and os.path.isabs(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return DEFAULT_SHELL

    def prepare_shell(self) -> ShellLaunch:
        """:meth:`shell_program` in ``$HOME`` with ``hub_environment()`` + ``TERM`` and a UTF-8 locale
        (:func:`utf8_locale`)."""
        shell = self.shell_program()
        if not os.access(shell, os.X_OK):
            raise BoardLaunchError("shell_unavailable", shell)
        env = utf8_locale(hub_environment(self.environment))
        env["TERM"] = DISPLAY_TERM
        return ShellLaunch([shell], env, str(omp_home.environment_home(self.environment)))

    def _check(self, started: AgentLaunch, cancel: threading.Event | None) -> str | None:
        omp = started.argv[0]
        try:
            version = launcher.omp_version(omp)
        except launcher.StartRequirementError as exc:
            verdict = f"check_failed:{exc}"[:REASON_LIMIT]
            self.record_event({"event": "start", "started_at": _stamp(started.started_at),
                               "resume": "--continue" in started.argv, "isolation": verdict}, started.home)
            return verdict
        environment = {**started.env, **omp_home.withheld_user_values(hub_environment(self.environment))}
        result = launcher.check_isolation([item for item in started.argv if item != "--continue"],
                                          cwd=started.cwd, environment=environment, role=CHECK_ROLE,
                                          allowed_skills=(SKILL_NAME,), omp_version=version,
                                          timeout=self.check_timeout,
                                          cancel=cancel)
        verdict = isolation_failure(result)
        if cancel is not None and cancel.is_set():  # stopped or deleted meanwhile: its home may be gone
            return verdict
        cleanup = result.get("cleanup") or {}
        self.record_event({"event": "start", "started_at": _stamp(started.started_at),
                           "resume": "--continue" in started.argv, "omp_version": version,
                           "isolation": verdict or "ok", "check_state": result.get("state"),
                           "check_stop": {key: cleanup.get(key) for key in ("stage", "elapsed", "returncode")}},
                          started.home)
        return verdict

    def record_event(self, event: Mapping[str, Any], home: Path | str | None = None) -> None:
        """One line in ``<agent home>/omp-logs/board-events.log`` (``board-agent/`` without a home); never raises
        (diagnostics only)."""
        try:
            record_board_event(home if home is not None else board_dir(self.state_dir),
                               {"at": _stamp(self.clock()), **event})
        except Exception as exc:  # noqa: BLE001
            _log(f"Board event not recorded: {type(exc).__name__}: {exc}")

    async def check_agent(self, started: AgentLaunch, cancel: threading.Event | None = None) -> str | None:
        """The start-up isolation check (``launcher.check_isolation``): None when nothing leaks and ``wb-inbox`` is
        visible, else why the Board agent must end (``isolation_failed`` detail). Fails closed when the check cannot
        run."""
        return await self._blocking(self._check, started, cancel)

    async def close(self) -> None:
        if self.listener is not None:
            await self.listener.stop()
