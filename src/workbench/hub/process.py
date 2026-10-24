"""Hub identity: ``hub.json``, the kernel lock holder and the client judgement.

``HubIdentity.state`` follows SPEC "`hub.json` schema": ``not_running`` when nobody
holds ``hub.lock`` (a leftover record is stale), ``running`` only when the lock holder,
record, live process, socket peer and ``welcome`` all agree, else ``unknown``.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import select
import signal
import sys
import time
from typing import Any, Callable

from workbench.backend.paths import read_private_json

from .paths import HubLayout, hub_layout

RECORD_SCHEMA_VERSION = 1
MODES = ("spawned", "foreground", "systemd")
VERSION_KINDS = ("git", "editable", "other", "unknown")

NOT_RUNNING, RUNNING, UNKNOWN = "not_running", "running", "unknown"


def start_ticks(pid: int) -> int | None:
    """Field 22 (starttime) of ``/proc/<pid>/stat``; None when unreadable."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
        return int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def installed_version() -> dict[str, Any]:
    """``{release, commit, kind}`` from the running distribution (doctor's source).

    A source checkout run through ``PYTHONPATH`` (no matching distribution) is
    ``unknown``: metadata of some other install must not describe this code.
    """
    unknown = {"release": "unknown", "commit": None, "kind": "unknown"}
    try:
        distribution = importlib.metadata.distribution("workbench")
        located = Path(distribution.locate_file("workbench/__init__.py")).resolve()
        running = Path(sys.modules["workbench"].__file__).resolve()
        if located != running:
            return unknown
        release = distribution.version
        source = json.loads(distribution.read_text("direct_url.json") or "{}")
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return unknown
    vcs = source.get("vcs_info") if isinstance(source, dict) else None
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        commit = vcs.get("commit_id")
        return {"release": release, "commit": commit if isinstance(commit, str) else None, "kind": "git"}
    dir_info = source.get("dir_info") if isinstance(source, dict) else None
    if isinstance(dir_info, dict) and dir_info.get("editable"):
        return {"release": release, "commit": None, "kind": "editable"}
    return {"release": release, "commit": None, "kind": "other"}


def read_hub_record(layout: HubLayout) -> dict[str, Any] | None:
    """The private ``hub.json`` when it has the schema-1 shape, else None."""
    value = read_private_json(layout.record)
    if value is None or value.get("schema_version") != RECORD_SCHEMA_VERSION:
        return None
    for key in ("pid", "start_ticks", "uid"):
        if type(value.get(key)) is not int or value[key] < 0:
            return None
    if not isinstance(value.get("socket"), str) or not isinstance(value.get("boot_id"), str):
        return None
    return value


class HubNotRunning(RuntimeError):
    """Nobody holds ``hub.lock``; any ``hub.json`` left behind is stale."""


class HubUnknown(RuntimeError):
    """The lock is held but the Hub identity could not be proven (``reason``)."""

    def __init__(self, reason: str, record: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason, self.record = reason, record


class HubUnresponsive(HubUnknown):
    """Lock holder, record, live process and socket peer agree, but no answer came in time."""


@dataclass(frozen=True)
class HubIdentity:
    state: str  # not_running | running | unknown
    layout: HubLayout
    record: dict[str, Any] | None = None
    reason: str | None = None
    welcome: dict[str, Any] | None = None


def lock_holder(path: Path) -> int | None:
    """The pid ``/proc/locks`` names for an exclusive flock on exactly this inode.

    Read-only: probing with ``flock`` could make a starting Hub lose its lock.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for row in Path("/proc/locks").read_text().splitlines():
        parts = row.split()
        if len(parts) >= 8 and parts[1:4] == ["FLOCK", "ADVISORY", "WRITE"]:
            major, minor, inode = parts[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                return int(parts[4])
    return None


def verified_record(layout: HubLayout) -> dict[str, Any]:
    """The record of the live lock holder; raises :class:`HubNotRunning`/:class:`HubUnknown`.

    Checks lock holder == record pid, record uid/boot == current and exact
    ``process_state(pid, start_ticks) == "alive"``. Connection checks are the client's.
    """
    from workbench.backend.process_identity import process_state
    try:
        holder = lock_holder(layout.lock)
    except (OSError, ValueError, IndexError) as exc:
        raise HubUnknown("lock_unreadable") from exc
    if holder is None:
        raise HubNotRunning(str(layout.lock))
    try:
        record = read_hub_record(layout)
    except OSError as exc:
        raise HubUnknown("record_unreadable") from exc
    if record is None:
        raise HubUnknown("record_missing" if not os.path.lexists(layout.record) else "record_invalid")
    if record["pid"] != holder:
        raise HubUnknown("lock_holder_mismatch", record)
    if record["uid"] != os.geteuid():
        raise HubUnknown("uid_mismatch", record)
    if process_state(record["pid"], record["start_ticks"], uid=record["uid"], boot_id=record["boot_id"]) != "alive":
        raise HubUnknown("process_mismatch", record)
    return record


def inspect_hub(layout: HubLayout | None = None, *, timeout: float = 5.0) -> HubIdentity:
    """Full client judgement (record, lock holder, process, ``SO_PEERCRED``, ``welcome``)."""
    from .client import HubClient
    layout = hub_layout() if layout is None else layout
    try:
        with HubClient(layout, timeout=timeout) as client:
            return client.identity
    except HubNotRunning:
        return HubIdentity(NOT_RUNNING, layout)
    except HubUnknown as exc:
        return HubIdentity(UNKNOWN, layout, exc.record, exc.reason)


RESPONSE_TIMEOUT = 5.0
RELEASE_TIMEOUT = 10.0
TERM_TIMEOUT = 5.0


@dataclass(frozen=True)
class StopResult:
    status: str  # stopped | not_running | refused | unknown | failed
    pid: int | None = None
    reason: str | None = None
    signalled: bool = False


def _released(layout: HubLayout, pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if lock_holder(layout.lock) != pid:
                return True
        except (OSError, ValueError, IndexError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _terminate(layout: HubLayout, record: dict[str, Any], reason: str) -> StopResult:
    """SIGTERM through a pidfd, only to the exact recorded, still lock-holding Hub."""
    from workbench.backend.process_identity import process_state
    pid = record["pid"]
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return StopResult("stopped", pid, reason) if _released(layout, pid, 0) else StopResult("unknown", pid, "ended")
    except OSError:
        return StopResult("unknown", pid, "pidfd_unavailable")
    try:
        # Re-prove the identity after pinning, so a recycled pid is never signalled.
        try:
            holder = lock_holder(layout.lock)
        except (OSError, ValueError, IndexError):
            holder = None
        if (holder != pid or process_state(pid, record["start_ticks"], uid=record["uid"],
                                           boot_id=record["boot_id"]) != "alive"):
            return StopResult("unknown", pid, "identity_changed")
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        exited = bool(select.select([pidfd], [], [], TERM_TIMEOUT)[0])
    except OSError:
        return StopResult("failed", pid, "signal_failed")
    finally:
        os.close(pidfd)
    if exited and _released(layout, pid, 1.0):
        return StopResult("stopped", pid, reason, signalled=True)
    return StopResult("failed", pid, "no_exit_after_sigterm", signalled=True)


def stop_hub(ask: Callable[[list], bool], layout: HubLayout | None = None) -> StopResult:
    """Two-phase stop shared by ``wb hub stop`` and ``wb update`` (SPEC "정지 2단계").

    ``ask(active)`` is called only when ``stop_request`` lists live owned things;
    a refusal stops nothing. A Hub whose identity was proven but that does not
    answer gets a pidfd SIGTERM; never SIGKILL.
    """
    from .client import HubClient
    layout = hub_layout() if layout is None else layout
    try:
        client = HubClient(layout, name="workbench-hub-stop", timeout=RESPONSE_TIMEOUT)
    except HubNotRunning:
        return StopResult("not_running")
    except HubUnresponsive as exc:
        return _terminate(layout, exc.record, exc.reason)
    except HubUnknown as exc:
        return StopResult("unknown", (exc.record or {}).get("pid"), exc.reason)
    record = client.record
    with client:
        try:
            offer = client.request("stop_request")
            if not offer.get("ok"):
                return StopResult("failed", record["pid"], offer.get("reason"))
            active = offer.get("active") or []
            if active and not ask(active):
                return StopResult("refused", record["pid"], "not_confirmed")
            reply = client.request("stop_confirm", token=offer.get("token"))
        except HubUnresponsive as exc:
            return _terminate(layout, record, exc.reason)
        except HubUnknown as exc:
            return StopResult("unknown", record["pid"], exc.reason)
    if not reply.get("ok"):
        return StopResult("failed", record["pid"], reply.get("reason"))
    if _released(layout, record["pid"], RELEASE_TIMEOUT):
        return StopResult("stopped", record["pid"])
    return _terminate(layout, record, "release_timeout")
