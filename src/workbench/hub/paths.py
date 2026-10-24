"""Hub state/runtime locations (SPEC "위치") and their directory safety.

The control socket never lives under ``omp-workbench/``: ``maintenance.discover()``
treats every directory there as a backend runtime binding. Clients read the socket
location from ``hub.json`` only; :func:`prepare_socket_path` is for the Hub itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Mapping

from workbench.backend.paths import APP_DIR_NAME, MAX_SOCKET_PATH_BYTES, DataDirError, ensure_private_dir

HUB_DIR_NAME = "hub"
RUNTIME_DIR_NAME = "omp-workbench-hub"
SOCKET_NAME = "hub.sock"
FALLBACK_BASE = Path("/tmp")  # SPEC fixed fallback; a keyword only so tests need not touch /tmp


@dataclass(frozen=True, slots=True)
class HubLayout:
    state_dir: Path

    @property
    def lock(self) -> Path: return self.state_dir / "hub.lock"
    @property
    def record(self) -> Path: return self.state_dir / "hub.json"
    @property
    def log(self) -> Path: return self.state_dir / "hub.log"
    @property
    def registry(self) -> Path: return self.state_dir / "registry.json"


def _home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or os.path.expanduser("~"))


def hub_layout(environment: Mapping[str, str] | None = None) -> HubLayout:
    """``$XDG_STATE_HOME/omp-workbench/hub`` when absolute, else ``~/.local/state/...``."""
    environment = os.environ if environment is None else environment
    state_home = environment.get("XDG_STATE_HOME", "")
    base = Path(state_home) if state_home and os.path.isabs(state_home) else _home(environment) / ".local" / "state"
    return HubLayout(Path(os.path.abspath(base / APP_DIR_NAME / HUB_DIR_NAME)))


def _user_only_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) & 0o077 == 0)


def socket_dir_candidates(environment: Mapping[str, str] | None = None, *,
                          fallback_base: Path = FALLBACK_BASE) -> list[Path]:
    """In order: ``$XDG_RUNTIME_DIR/omp-workbench-hub`` when that base is absolute and
    user-only, then ``/tmp/omp-workbench-hub-<uid>``."""
    environment = os.environ if environment is None else environment
    runtime = environment.get("XDG_RUNTIME_DIR", "")
    candidates = []
    if runtime and os.path.isabs(runtime) and _user_only_dir(Path(runtime)):
        candidates.append(Path(os.path.abspath(runtime)) / RUNTIME_DIR_NAME)
    candidates.append(Path(fallback_base) / f"{RUNTIME_DIR_NAME}-{os.geteuid()}")
    return candidates


def prepare_state_dir(layout: HubLayout) -> Path:
    return ensure_private_dir(layout.state_dir)


def prepare_socket_path(environment: Mapping[str, str] | None = None, *, fallback_base: Path = FALLBACK_BASE) -> Path:
    """Create (0700) only the first candidate whose socket path fits AF_UNIX."""
    candidates = socket_dir_candidates(environment, fallback_base=fallback_base)
    for directory in candidates:
        if len(os.fsencode(directory / SOCKET_NAME)) <= MAX_SOCKET_PATH_BYTES:
            return ensure_private_dir(directory) / SOCKET_NAME
    raise DataDirError("hub socket path is too long for AF_UNIX: "
                       + ", ".join(str(directory / SOCKET_NAME) for directory in candidates))
