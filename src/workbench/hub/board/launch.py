"""Board agent OMP launch: home, system key, internal MCP listener, overlay, argv and isolation check.

Task Inbox SPEC "Board agent 세트", ADR-0003 and the TI-15 probe (PROBE-OMP-MCP "TI-16이 쓸 구성").
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import functools
import json
import os
from pathlib import Path
import re
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
from workbench.backend.paths import ensure_private_dir, write_private_bytes

from .. import auth, mcp
from ..config import BoardAgentConfig
from ..process import hub_environment

BOARD_DIR_NAME = "board-agent"
EMPTY_DIR_NAME = "empty"
OVERLAY_NAME = "omp-board.yml"
MCP_FILE_NAME = "mcp.json"
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
SERVER_NAME = "workbench-hub"
# OMP names an MCP tool ``mcp__<server, '-' -> '_'>_<tool>`` (TI-15).
HUB_TOOL_PREFIX = "mcp__" + SERVER_NAME.replace("-", "_") + "_"
KEY_ENV = "WB_BOARD_MCP_KEY"
CHECK_ROLE = "board"
DEFAULT_SHELL = "/bin/sh"
REASON_LIMIT = 256  # the audit ``reason`` limit
MESSAGE_LIMIT = 1024  # the human ``message`` next to a failed channel
FALLBACK_LOCALE = "C.UTF-8"
LOCALE_KEYS = ("LC_ALL", "LC_CTYPE", "LANG")  # the C library's precedence for the character type
_UTF8_LOCALE = re.compile(r"\.utf-?8(@|$)", re.IGNORECASE)
# The isolation list without ``native``: the only provider that reads ``<agent dir>/mcp.json`` (TI-15).
BOARD_PROVIDER_IDS = tuple(item for item in ISOLATION_PROVIDER_IDS if item != "native")


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
class AgentLaunch:
    """One Board agent start. ``env`` carries the system key; never log or audit it."""

    argv: list[str]
    env: dict[str, str] = field(repr=False)  # holds the raw system key
    cwd: str = ""
    key_id: str = ""
    started_at: float = 0.0  # wall clock of prepare_agent (the Board events line)


@dataclass(frozen=True, slots=True)
class ShellLaunch:
    argv: list[str]
    env: dict[str, str]
    cwd: str


def board_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / BOARD_DIR_NAME


def prepare_board_home(state_dir: Path | str, environment: Mapping[str, str]) -> omp_home.OmpHome:
    """``<hub state>/board-agent/omp-root`` with ``agent.db`` linked to the user's store (``omp_home`` rules).

    No Workbench skills or subagent definitions: both point at an empty private directory.
    """
    root = ensure_private_dir(board_dir(state_dir))
    empty = ensure_private_dir(root / EMPTY_DIR_NAME)
    return omp_home.prepare_omp_home(root, environment, skills_dir=empty, provider_ids=BOARD_PROVIDER_IDS,
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


def archive_omp_logs(state_dir: Path | str, home: omp_home.OmpHome) -> list[str]:
    """Copy the Board home's OMP logs (regular files this user owns, never through a symlink) into
    ``board-agent/omp-logs`` (0700, files 0600, redacted, bounded) and keep the newest :data:`OMP_LOG_KEEP` there.
    Returns the names copied."""
    archive = ensure_private_dir(board_dir(state_dir) / OMP_LOG_ARCHIVE_NAME)
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


def record_board_event(state_dir: Path | str, event: Mapping[str, Any]) -> None:
    """Append one redacted JSON line to ``board-agent/omp-logs/board-events.log`` (0600, never through a symlink);
    past :data:`OMP_EVENTS_LIMIT` bytes only the newer half of the lines is kept."""
    archive = ensure_private_dir(board_dir(state_dir) / OMP_LOG_ARCHIVE_NAME)
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


def board_overlay() -> dict[str, Any]:
    """The second ``--config`` after ``omp-isolation.yml`` (PROBE-OMP-MCP: a later array replaces the earlier one).

    Only ``native`` comes back (it reads ``<agent dir>/mcp.json``); its project-level MCP files stay off, no
    Workbench skills, OMP's ``seance`` agent off as for the manager/worker, memory back on (D-25).
    """
    return {"disabledProviders": list(BOARD_PROVIDER_IDS), "mcp": {"enableProjectConfig": False},
            "skills": {"customDirectories": [], "includeSkills": [], "ignoredSkills": []},
            "task": {"disabledAgents": ["seance"]},
            "memory": {"backend": "local"}, "memories": {"enabled": True}}


def mcp_config(url: str) -> dict[str, Any]:
    """``<agent dir>/mcp.json``: the Hub server only; the key is an environment reference, never the value."""
    return {"mcpServers": {SERVER_NAME: {"type": "http", "url": url,
                                         "headers": {"Authorization": f"Bearer ${{{KEY_ENV}}}"}}}}


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


def write_board_files(state_dir: Path | str, home: omp_home.OmpHome, url: str) -> Path:
    """Rewrite the overlay and ``mcp.json`` (0600) at every start; returns the overlay path."""
    overlay = board_dir(state_dir) / OVERLAY_NAME
    write_private_bytes(overlay, _json_bytes(board_overlay()))
    write_private_bytes(home.agent_dir / MCP_FILE_NAME, _json_bytes(mcp_config(url)))
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
    """None when the checked OMP showed nothing but Hub MCP tools (``mcp:mcp__workbench_hub_*``) beyond the
    isolation baseline; otherwise the reason (other leaks named, or why the check itself failed)."""
    if result.get("error") or result.get("observed") is None:
        return f"check_failed:{result.get('error') or result.get('state')}"[:REASON_LIMIT]
    leaks = [leak for leak in result.get("leaks") or [] if not leak.startswith("mcp:" + HUB_TOOL_PREFIX)]
    if not leaks:
        return None
    return ("leak:" + ",".join(leaks))[:REASON_LIMIT]


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


def board_environment(base: Mapping[str, str], home: omp_home.OmpHome, key: str) -> dict[str, str]:
    """``hub_environment()`` with the Board home values, ``TERM``, a UTF-8 locale and the system key (HOME stays
    the user's)."""
    env = utf8_locale(omp_home.home_environment(hub_environment(base), home.environment()))
    env["TERM"] = DISPLAY_TERM
    env[KEY_ENV] = key
    return env


class BoardLauncher:
    """Prepares each Board agent start; owned by the Hub's ``BoardSet``.

    ``api_keys`` returns the Hub's key store (``hub.sqlite3``); ``listener`` is the Board agent's
    internal MCP listener (TI-05), or a callable that makes it on the first start (the key store
    may open only then); it is started on the first start and stopped by :meth:`close`.
    """

    def __init__(self, state_dir: Path | str, config: BoardAgentConfig, *,
                 api_keys: Callable[[], auth.ApiKeyStore],
                 listener: mcp.InternalListener | Callable[[], mcp.InternalListener],
                 environment: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time,
                 check_timeout: float = launcher.ISOLATION_CHECK_TIMEOUT):
        self.state_dir = Path(state_dir)
        self.check_timeout = check_timeout
        self.config = config
        self.api_keys = api_keys
        self._listener_source = listener
        self.listener: mcp.InternalListener | None = listener if isinstance(listener, mcp.InternalListener) else None
        self.environment = dict(os.environ if environment is None else environment)
        self.clock = clock

    async def _blocking(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)

    def _home(self) -> omp_home.OmpHome:
        try:
            return prepare_board_home(self.state_dir, self.environment)
        except (omp_home.OmpHomeError, OSError) as exc:
            raise BoardLaunchError("home_unavailable", str(exc)) from exc

    def _keep_logs(self, home: omp_home.OmpHome) -> None:
        try:
            archive_omp_logs(self.state_dir, home)
        except Exception as exc:  # noqa: BLE001 - diagnostics only: never blocks a start (DataDirError, OSError, ...)
            _log(f"OMP log copy skipped: {type(exc).__name__}: {exc}")

    def _rotate(self) -> auth.IssuedKey:
        try:
            return self.api_keys().rotate_system_key(now=float(self.clock()))
        except Exception as exc:  # noqa: BLE001 - HubDbError, sqlite3, a missing store
            raise BoardLaunchError("hub_db_unavailable", type(exc).__name__) from exc

    async def prepare_agent(self) -> AgentLaunch:
        """Home, new system key, internal listener, overlay/``mcp.json`` and argv for one start."""
        omp = resolve_omp(self.environment)
        prompt = role_prompt(self.config)
        model = board_model(self.config)
        home = await self._blocking(self._home)
        await self._blocking(self._keep_logs, home)
        resume = await self._blocking(previous_session, home)
        issued = await self._blocking(self._rotate)
        try:
            if self.listener is None:
                self.listener = self._listener_source()
            url = await self.listener.start()
        except RuntimeError as exc:  # the Hub's key store is not open (web services stopped)
            raise BoardLaunchError("hub_db_unavailable", str(exc)) from exc
        except OSError as exc:
            raise BoardLaunchError("internal_listener_failed", str(exc)) from exc
        try:
            overlay = await self._blocking(write_board_files, self.state_dir, home, url)
        except OSError as exc:
            raise BoardLaunchError("home_unavailable", str(exc)) from exc
        env = board_environment(self.environment, home, issued.raw)
        cwd = str(omp_home.environment_home(self.environment))
        return AgentLaunch(board_argv(omp, overlay, prompt, resume=resume, model=model), env, cwd, issued.key_id,
                           float(self.clock()))

    def prepare_shell(self) -> ShellLaunch:
        """``board_agent.shell`` / ``$SHELL`` / ``/bin/sh`` in ``$HOME`` with ``hub_environment()`` + ``TERM`` and a
        UTF-8 locale (:func:`utf8_locale`)."""
        shell = self.config.shell or self.environment.get("SHELL") or DEFAULT_SHELL
        if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
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
                               "resume": "--continue" in started.argv, "isolation": verdict})
            return verdict
        environment = {**started.env, **omp_home.withheld_user_values(hub_environment(self.environment))}
        result = launcher.check_isolation([item for item in started.argv if item != "--continue"],
                                          cwd=started.cwd, environment=environment, role=CHECK_ROLE,
                                          allowed_skills=(), omp_version=version, timeout=self.check_timeout,
                                          cancel=cancel)
        verdict = isolation_failure(result)
        cleanup = result.get("cleanup") or {}
        self.record_event({"event": "start", "started_at": _stamp(started.started_at),
                           "resume": "--continue" in started.argv, "omp_version": version,
                           "isolation": verdict or "ok", "check_state": result.get("state"),
                           "check_stop": {key: cleanup.get(key) for key in ("stage", "elapsed", "returncode")}})
        return verdict

    def record_event(self, event: Mapping[str, Any]) -> None:
        """One line in ``board-agent/omp-logs/board-events.log``; never raises (diagnostics only)."""
        try:
            record_board_event(self.state_dir, {"at": _stamp(self.clock()), **event})
        except Exception as exc:  # noqa: BLE001
            _log(f"Board event not recorded: {type(exc).__name__}: {exc}")

    async def check_agent(self, started: AgentLaunch, cancel: threading.Event | None = None) -> str | None:
        """The start-up isolation check (``launcher.check_isolation``): None when only Hub MCP tools are visible,
        else why the Board agent must end (``isolation_failed`` detail). Fails closed when the check cannot run."""
        return await self._blocking(self._check, started, cancel)

    async def close(self) -> None:
        if self.listener is not None:
            await self.listener.stop()
