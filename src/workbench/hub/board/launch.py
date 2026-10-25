"""Board agent OMP launch: home, system key, internal MCP listener, overlay, argv and isolation check.

Task Inbox SPEC "Board agent 세트", ADR-0003 and the TI-15 probe (PROBE-OMP-MCP "TI-16이 쓸 구성").
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
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
SERVER_NAME = "workbench-hub"
# OMP names an MCP tool ``mcp__<server, '-' -> '_'>_<tool>`` (TI-15).
HUB_TOOL_PREFIX = "mcp__" + SERVER_NAME.replace("-", "_") + "_"
KEY_ENV = "WB_BOARD_MCP_KEY"
CHECK_ROLE = "board"
DEFAULT_SHELL = "/bin/sh"
REASON_LIMIT = 256  # the audit ``reason`` limit
# The isolation list without ``native``: the only provider that reads ``<agent dir>/mcp.json`` (TI-15).
BOARD_PROVIDER_IDS = tuple(item for item in ISOLATION_PROVIDER_IDS if item != "native")


class BoardLaunchError(RuntimeError):
    """The Board agent OMP cannot be started; ``reason`` is the short status detail."""

    def __init__(self, reason: str, text: str = ""):
        super().__init__(text or reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AgentLaunch:
    """One Board agent start. ``env`` carries the system key; never log or audit it."""

    argv: list[str]
    env: dict[str, str] = field(repr=False)  # holds the raw system key
    cwd: str = ""
    key_id: str = ""


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
        raise BoardLaunchError("omp_unavailable", str(exc)) from exc
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


def board_environment(base: Mapping[str, str], home: omp_home.OmpHome, key: str) -> dict[str, str]:
    """``hub_environment()`` with the Board home values, ``TERM`` and the system key (HOME stays the user's)."""
    env = omp_home.home_environment(hub_environment(base), home.environment())
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
        return AgentLaunch(board_argv(omp, overlay, prompt, resume=resume, model=model), env, cwd, issued.key_id)

    def prepare_shell(self) -> ShellLaunch:
        """``board_agent.shell`` / ``$SHELL`` / ``/bin/sh`` in ``$HOME`` with ``hub_environment()`` + ``TERM``."""
        shell = self.config.shell or self.environment.get("SHELL") or DEFAULT_SHELL
        if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
            raise BoardLaunchError("shell_unavailable", shell)
        env = hub_environment(self.environment)
        env["TERM"] = DISPLAY_TERM
        return ShellLaunch([shell], env, str(omp_home.environment_home(self.environment)))

    def _check(self, started: AgentLaunch, cancel: threading.Event | None) -> str | None:
        omp = started.argv[0]
        try:
            version = launcher.omp_version(omp)
        except launcher.StartRequirementError as exc:
            return f"check_failed:{exc}"[:REASON_LIMIT]
        environment = {**started.env, **omp_home.withheld_user_values(hub_environment(self.environment))}
        result = launcher.check_isolation([item for item in started.argv if item != "--continue"],
                                          cwd=started.cwd, environment=environment, role=CHECK_ROLE,
                                          allowed_skills=(), omp_version=version, timeout=self.check_timeout,
                                          cancel=cancel)
        return isolation_failure(result)

    async def check_agent(self, started: AgentLaunch, cancel: threading.Event | None = None) -> str | None:
        """The start-up isolation check (``launcher.check_isolation``): None when only Hub MCP tools are visible,
        else why the Board agent must end (``isolation_failed`` detail). Fails closed when the check cannot run."""
        return await self._blocking(self._check, started, cancel)

    async def close(self) -> None:
        if self.listener is not None:
            await self.listener.stop()
