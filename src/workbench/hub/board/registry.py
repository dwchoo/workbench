"""The Board terminal registry (web-board-ux SPEC s4 "데이터 모델: terminal registry", WBX-D44..D48).

``<hub state>/board-agents/registry.json`` holds the Board agents and the two limits; shells are never stored (they end
with the Hub run). The file is this user's 0600 regular file in a 0700 directory, replaced atomically (temp file, fsync,
rename). A file that cannot be read or breaks a rule is :class:`RegistryInvalid`: the Board starts nothing and the file
is never overwritten. Without a file the first Board is ``agent-1`` in the existing ``board-agent/`` home (the
migration of the single Board agent; a fresh install gets the same).

``hub.sqlite3`` gets no table: ``auth.HubDb.open`` refuses another schema version, so a downgrade could not open it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import secrets
import stat
import unicodedata
from typing import Callable, Iterable

from workbench.backend.paths import DataDirError, ensure_private_dir, write_private_bytes

DIR_NAME = "board-agents"
FILE_NAME = "registry.json"
SCHEMA_VERSION = 1
MIGRATED_HOME = "board-agent"  # the single Board agent's home before Track D: ``agent-1`` keeps it
AGENT_RANGE = (1, 8)
SHELL_RANGE = (1, 32)
DEFAULT_AGENT_LIMIT, DEFAULT_SHELL_LIMIT = 4, 10
NAME_MAX = 40
RESERVED_NAME = "board-agent"  # orchestrator decision 2026-10-29: no terminal takes this name (any case)
KINDS = ("agent", "shell")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})  # Cs: a lone surrogate (JSON "\ud800")
_ID = {"agent": re.compile(r"^a-[0-9a-f]{12}\Z"), "shell": re.compile(r"^s-[0-9a-f]{12}\Z")}
_DEFAULT = re.compile(r"^(agent|shell)-([1-9][0-9]*)\Z")
FIX = ("Fix or move the file away (the Board then starts over with agent-1 in board-agent/), "
       "then restart the Hub (wb hub stop; wb hub start).")


class NameRefused(ValueError):
    """``name_invalid`` (empty, too long, a control character, not text) or ``name_taken`` (same kind, casefold)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RegistryInvalid(Exception):
    """The registry file cannot be used; ``message`` names the file and the fix."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RegistryWriteFailed(Exception):
    """The new registry was not written (disk full, EIO, permissions); the old file is unchanged."""


@dataclass(frozen=True, slots=True)
class Options:
    agent_limit: int = DEFAULT_AGENT_LIMIT
    shell_limit: int = DEFAULT_SHELL_LIMIT

    def as_dict(self) -> dict:
        return {"agent_limit": self.agent_limit, "shell_limit": self.shell_limit}


@dataclass(frozen=True, slots=True)
class AgentEntry:
    id: str
    name: str
    home: str  # relative to ``<hub state>``: ``board-agent`` or ``board-agents/<id>``
    created_at: float

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "home": self.home, "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class Registry:
    options: Options = Options()
    agents: list = field(default_factory=list)

    def with_agents(self, agents: Iterable[AgentEntry]) -> "Registry":
        return replace(self, agents=list(agents))

    def as_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "options": self.options.as_dict(),
                "agents": [agent.as_dict() for agent in self.agents]}


# --- names ---------------------------------------------------------------------------------------

def check_name(value: object, others: Iterable[str]) -> str:
    """The stored form of ``value`` (trimmed) or :class:`NameRefused`; ``others`` are the kind's other names."""
    if not isinstance(value, str):
        raise NameRefused("name_invalid")
    name = value.strip()
    if not 1 <= len(name) <= NAME_MAX or any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in name):
        raise NameRefused("name_invalid")
    folded = name.casefold()
    if folded == RESERVED_NAME:  # the pre-Track-D author name: older rows keep reading "Board agent"
        raise NameRefused("name_invalid")
    if any(folded == other.casefold() for other in others):
        raise NameRefused("name_taken")
    return name


def default_name(kind: str, names: Iterable[str]) -> str:
    """``<kind>-N`` with the smallest N >= 1 that no name of the kind uses (casefold)."""
    used = set()
    for name in names:
        match = _DEFAULT.match(name.casefold())
        if match and match.group(1) == kind:
            used.add(int(match.group(2)))
    number = 1
    while number in used:
        number += 1
    return f"{kind}-{number}"


def new_id(kind: str, taken: Iterable[str] = ()) -> str:
    taken = set(taken)
    while True:
        value = f"{kind[0]}-{secrets.token_hex(6)}"
        if value not in taken:
            return value


def agent_home(agent_id: str) -> str:
    return f"{DIR_NAME}/{agent_id}"


# --- file ----------------------------------------------------------------------------------------

def path_of(state_dir: Path | str) -> Path:
    return Path(state_dir) / DIR_NAME / FILE_NAME


def _invalid(path: Path, why: str) -> RegistryInvalid:
    return RegistryInvalid(f"Board registry {path} is unusable ({why}). {FIX}")


def _int_in(value: object, bounds: tuple[int, int]) -> bool:
    return type(value) is int and bounds[0] <= value <= bounds[1]


def _parse(path: Path, raw: bytes) -> Registry:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _invalid(path, "not JSON") from None
    if not isinstance(value, dict) or set(value) != {"schema_version", "options", "agents"}:
        raise _invalid(path, "not a registry object")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise _invalid(path, "another schema_version")
    options = value["options"]
    if (not isinstance(options, dict) or set(options) != {"agent_limit", "shell_limit"}
            or not _int_in(options["agent_limit"], AGENT_RANGE) or not _int_in(options["shell_limit"], SHELL_RANGE)):
        raise _invalid(path, "limits out of range")
    agents = value["agents"]
    if not isinstance(agents, list):
        raise _invalid(path, "agents is not a list")
    entries: list[AgentEntry] = []
    for item in agents:
        if not isinstance(item, dict) or set(item) != {"id", "name", "home", "created_at"}:
            raise _invalid(path, "an agent entry has other fields")
        agent_id, name, home, created = item["id"], item["name"], item["home"], item["created_at"]
        if not isinstance(agent_id, str) or not _ID["agent"].match(agent_id):
            raise _invalid(path, "an agent id is not a-<12 hex>")
        if any(entry.id == agent_id for entry in entries):
            raise _invalid(path, "an agent id repeats")
        if home not in (MIGRATED_HOME, agent_home(agent_id)) or (
                home == MIGRATED_HOME and any(entry.home == MIGRATED_HOME for entry in entries)):
            raise _invalid(path, "an agent home is not board-agent or board-agents/<its id>")
        if type(created) not in (int, float) or created != created:
            raise _invalid(path, "created_at is not a number")
        try:
            stored = check_name(name, [entry.name for entry in entries])
        except NameRefused as exc:
            raise _invalid(path, f"an agent name breaks the name rule ({exc.reason})") from None
        if stored != name:
            raise _invalid(path, "an agent name has surrounding spaces")
        entries.append(AgentEntry(agent_id, name, home, float(created)))
    if len(entries) > options["agent_limit"]:
        raise _invalid(path, "more agents than agent_limit")
    return Registry(Options(options["agent_limit"], options["shell_limit"]), entries)


def load(state_dir: Path | str) -> Registry | None:
    """The registry, None without a file, :class:`RegistryInvalid` for anything unusable. Blocking (executor)."""
    path = path_of(state_dir)
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _invalid(path, f"its directory cannot be opened: {exc.strerror}") from None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise _invalid(path, "its directory is not this user's 0700 directory")
        try:
            fd = os.open(FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _invalid(path, f"it cannot be opened (a symlink?): {exc.strerror}") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise _invalid(path, "it is not this user's 0600 regular file")
            if info.st_size > 1 << 20:
                raise _invalid(path, "it is too large")
            raw = os.read(fd, (1 << 20) + 1)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    return _parse(path, raw)


def save(state_dir: Path | str, data: Registry) -> None:
    """Replace the registry atomically (0700 directory, 0600 file); :class:`RegistryWriteFailed` leaves the old one."""
    path = path_of(state_dir)
    try:
        payload = (json.dumps(data.as_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RegistryWriteFailed(f"a value is not UTF-8 text: {exc.reason}") from None
    try:
        if not os.path.lexists(path.parent):
            ensure_private_dir(path.parent)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if os.fstat(directory).st_uid != os.geteuid():
                raise RegistryWriteFailed(f"{path.parent} is not this user's directory")
            write_private_bytes(path, payload, dir_fd=directory)
        finally:
            os.close(directory)
    except (DataDirError, OSError) as exc:
        raise RegistryWriteFailed(str(exc)) from exc


def load_or_create(state_dir: Path | str, *, now: Callable[[], float]) -> Registry:
    """:func:`load`, or (no file) a new registry with ``agent-1`` in the migrated ``board-agent`` home."""
    data = load(state_dir)
    if data is not None:
        return data
    data = Registry(Options(), [AgentEntry(new_id("agent"), default_name("agent", []), MIGRATED_HOME,
                                           float(now()))])
    save(state_dir, data)
    return data
