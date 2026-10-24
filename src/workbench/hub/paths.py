"""Hub state/runtime locations (SPEC "위치") and their directory safety.

The control socket never lives under ``omp-workbench/``: ``maintenance.discover()``
treats every directory there as a backend runtime binding. Clients read the socket
location from ``hub.json`` only; :func:`runtime_socket_dir` is for the Hub itself.
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


def runtime_socket_dir(environment: Mapping[str, str] | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/omp-workbench-hub`` when that base is absolute and user-only,
    else ``/tmp/omp-workbench-hub-<uid>``."""
    environment = os.environ if environment is None else environment
    runtime = environment.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isabs(runtime) and _user_only_dir(Path(runtime)):
        return Path(os.path.abspath(runtime)) / RUNTIME_DIR_NAME
    return Path("/tmp") / f"{RUNTIME_DIR_NAME}-{os.geteuid()}"


def prepare_state_dir(layout: HubLayout) -> Path:
    return ensure_private_dir(layout.state_dir)


def prepare_socket_path(environment: Mapping[str, str] | None = None) -> Path:
    directory = ensure_private_dir(runtime_socket_dir(environment))
    path = directory / SOCKET_NAME
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
        raise DataDirError(f"hub socket path is too long for AF_UNIX: {path}")
    return path
