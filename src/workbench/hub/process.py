"""Hub identity: ``hub.json``, the kernel lock holder and the client judgement.

``HubIdentity.state`` follows SPEC "`hub.json` schema": ``not_running`` when nobody
holds ``hub.lock`` (a leftover record is stale), ``running`` only when the lock holder,
record, live process, socket peer and ``welcome`` all agree, else ``unknown``.
"""
from __future__ import annotations

from dataclasses import dataclass
import enum
import importlib.metadata
import json
import os
from pathlib import Path
import select
import signal
import sys
import time
from typing import Any, Callable, Mapping

from workbench.backend.paths import flock_holder, read_private_json

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


def lock_holder(path: Path, candidate: int | None = None) -> int | None:
    """The pid holding an exclusive flock on exactly this inode, or None when it is free.

    ``candidate`` (the recorded Hub pid) is confirmed from its own ``/proc/<pid>/fdinfo``; otherwise
    ``/proc/locks`` decides, where one row is enough and absence must hold on several reads (a single
    read can skip a row under other processes' flock churn). Read-only: probing with ``flock`` could
    make a starting Hub lose its lock.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return flock_holder((os.major(info.st_dev), os.minor(info.st_dev), info.st_ino), candidate=candidate)


def verified_record(layout: HubLayout) -> dict[str, Any]:
    """The record of the live lock holder; raises :class:`HubNotRunning`/:class:`HubUnknown`.

    Checks lock holder == record pid, record uid/boot == current and exact
    ``process_state(pid, start_ticks) == "alive"``. Connection checks are the client's.
    """
    from workbench.backend.process_identity import process_state
    try:
        candidate = (read_hub_record(layout) or {}).get("pid")
    except (OSError, ValueError, AttributeError):
        candidate = None
    try:
        holder = lock_holder(layout.lock, candidate if type(candidate) is int else None)
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
            if lock_holder(layout.lock, pid) != pid:
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
            holder = lock_holder(layout.lock, pid)
        except (OSError, ValueError, IndexError):
            holder = None
        if (holder != pid or process_state(pid, record["start_ticks"], uid=record["uid"],
                                           boot_id=record["boot_id"]) != "alive"):
            return StopResult("unknown", pid, "identity_changed")
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        poller = select.poll()  # not select(): pidfd may be >= 1024
        poller.register(pidfd, select.POLLIN)
        exited = bool(poller.poll(int(TERM_TIMEOUT * 1000)))
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


# --- Spawn (SPEC "Hub 프로세스 수명" Spawn) -------------------------------------------

READY_TIMEOUT = 5.0
REPARENT_TIMEOUT = 2.0
READY_STARTED, READY_LOST, READY_MANAGED = b"R", b"L", b"M"
# Project/session-only variables the per-user Hub must not inherit (HB-T04). ``WB_BOARD_MCP_KEY`` is the Board
# agent's system key (``board.launch.KEY_ENV``): a Hub or a Board shell started from a Board terminal never gets it.
PROJECT_ENV = frozenset({"WORKBENCH_DATA_DIR", "WORKBENCH_AGENT_ROLES", "WORKBENCH_OMP_RUNTIME_ROOT",
                         "WB_BOARD_MCP_KEY"})
PROJECT_ENV_PREFIXES = ("WORKBENCH_G3_",)

# spawn_hub outcomes (also HubStart failure reasons)
SPAWN_STARTED, SPAWN_LOST, SPAWN_MANAGED = "started", "lost", "managed_tree"
SPAWN_UPDATE, SPAWN_STATE_ERROR, SPAWN_FAILED, SPAWN_TIMEOUT = (
    "update_in_progress", "state_error", "spawn_failed", "ready_timeout")
# Caller pre-check could not read its /proc ancestry (e.g. hidepid): held like managed_tree.
PROC_UNREADABLE = "proc_unreadable"


def hub_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """The caller's environment without project/session-only variables."""
    environment = os.environ if environment is None else environment
    return {key: value for key, value in environment.items()
            if key not in PROJECT_ENV and not key.startswith(PROJECT_ENV_PREFIXES)}


PROC = Path("/proc")
MAX_ANCESTRY = 4096
WALK_ATTEMPTS = 3  # an ancestor exiting mid-walk reparents us: walk again from the start
WALK_RETRY_DELAY = 0.02


class AncestryCheck(enum.IntEnum):
    """Non-pid ``managed_ancestor`` results. ``UNVERIFIABLE`` (``/proc`` cannot prove
    "outside", fail closed) is truthy and never a real pid; compare with ``is``."""

    UNVERIFIABLE = -1


UNVERIFIABLE = AncestryCheck.UNVERIFIABLE


def _managed_argv(argv: list[bytes]) -> bool:
    """A backend (``maintenance.discover()``'s rule: an ``_backend`` element and an
    element containing ``workbench``) or the shell supervisor (a ``--supervisor``
    element and an element ending with ``shell_g2/lifecycle.py``)."""
    if b"_backend" in argv and any(b"workbench" in value for value in argv):
        return True
    return b"--supervisor" in argv and any(value.endswith(b"shell_g2/lifecycle.py") for value in argv)


def managed_ancestor(pid: int, *, proc: Path = PROC) -> int | None:
    """The first process from ``pid`` up its ancestry that is WB-managed, or None.

    A failed ``/proc`` read restarts the walk (up to :data:`WALK_ATTEMPTS`), since an
    exiting ancestor is a transient ENOENT. If the chain still cannot be read, or it is
    longer than :data:`MAX_ANCESTRY`, the result is :data:`UNVERIFIABLE` ("inside").
    """
    for attempt in range(WALK_ATTEMPTS):
        if attempt:
            time.sleep(WALK_RETRY_DELAY)
        current = pid
        for _ in range(MAX_ANCESTRY):
            try:
                argv = (proc / str(current) / "cmdline").read_bytes().split(b"\0")
                parent = int((proc / str(current) / "stat").read_bytes().rsplit(b") ", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                break  # walk again from the start
            if _managed_argv(argv):
                return current
            if parent == 0:
                return None
            current = parent
        else:
            return UNVERIFIABLE  # bounded: a cycle or an absurd depth never proves "outside"
    return UNVERIFIABLE


def wait_reparented(spawner_pid: int | None, timeout: float = REPARENT_TIMEOUT) -> None:
    """Hub side: wait (bounded) until the spawn intermediate exited and we were reparented."""
    if spawner_pid is None:
        return
    deadline = time.monotonic() + timeout
    while os.getppid() == spawner_pid and time.monotonic() < deadline:
        time.sleep(0.01)


def send_ready(fd: int | None, code: bytes) -> None:
    """Hub side: report ``R``/``L``/``M`` once and close the ready fd (a gone reader is fine)."""
    if fd is None:
        return
    try:
        os.write(fd, code)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _max_fd() -> int:
    import resource
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return max(1 << 20 if soft == resource.RLIM_INFINITY else soft, 1 << 16)


def spawn_hub(layout: HubLayout | None = None, *, timeout: float = READY_TIMEOUT,
              environment: Mapping[str, str] | None = None) -> str:
    """Double-fork + ``setsid`` a ``wb _hub`` and return its ready outcome.

    Holds the user update fence (shared, non-blocking) only while spawning; the Hub
    never holds it. The Hub gets stdin ``/dev/null``, stdout/stderr ``hub.log``, cwd
    ``/``, umask 077, no other caller fd and :func:`hub_environment`. Returns
    ``started`` (``R``), ``lost`` (``L``: another Hub holds the lock), ``managed_tree``
    (``M``), ``update_in_progress``, ``state_error``, ``spawn_failed`` (EOF before a
    code) or ``ready_timeout``.
    """
    from workbench.backend.cli import _bootstrap_source
    from workbench.backend.maintenance import fence
    from workbench.backend.paths import DataDirError
    from .paths import prepare_state_dir
    layout = hub_layout() if layout is None else layout
    try:
        with fence(shared=True):
            prepare_state_dir(layout)
            return _spawn(layout, timeout, hub_environment(environment), _bootstrap_source())
    except DataDirError as exc:
        return SPAWN_UPDATE if isinstance(exc.__cause__, BlockingIOError) else SPAWN_STATE_ERROR
    except OSError:
        return SPAWN_STATE_ERROR


def _spawn(layout: HubLayout, timeout: float, environment: dict[str, str], bootstrap: str) -> str:
    import fcntl
    read_end, write_end = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        intermediate = os.fork()
    except OSError:
        os.close(read_end)
        os.close(write_end)
        return SPAWN_FAILED
    if intermediate == 0:  # pragma: no cover - child side
        try:
            os.close(read_end)
            os.setsid()
            spawner = os.getpid()
            if os.fork() == 0:
                ready = fcntl.fcntl(write_end, fcntl.F_DUPFD, 3)  # never 0-2
                devnull = os.open(os.devnull, os.O_RDWR)
                log = os.open(layout.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                os.dup2(devnull, 0)
                os.dup2(log, 1)
                os.dup2(log, 2)
                os.closerange(3, ready)
                os.closerange(ready + 1, _max_fd())
                os.set_inheritable(ready, True)
                os.chdir("/")
                os.umask(0o077)
                argv = [sys.executable, "-I", "-c", bootstrap, "_hub",
                        "--ready-fd", str(ready), "--spawner-pid", str(spawner)]
                os.execve(sys.executable, argv, environment)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(write_end)
    try:
        os.waitpid(intermediate, 0)
        poller = select.poll()  # not select(): the caller may already hold fds >= 1024
        poller.register(read_end, select.POLLIN)
        if not poller.poll(max(0, int(timeout * 1000))):
            return SPAWN_TIMEOUT
        code = os.read(read_end, 1)
    finally:
        os.close(read_end)
    return {READY_STARTED: SPAWN_STARTED, READY_LOST: SPAWN_LOST,
            READY_MANAGED: SPAWN_MANAGED}.get(code, SPAWN_FAILED)


@dataclass(frozen=True)
class HubStart:
    """``ensure_hub`` result; ``ok`` means a ``running`` Hub was proven (``identity``).

    ``reason`` when ok: ``already_running``, ``started``, ``joined`` (lost the lock race
    to a Hub that became running). Otherwise: ``update_in_progress``, ``managed_tree``,
    ``proc_unreadable``, ``hub_unknown``, ``state_error``, ``spawn_failed``, ``ready_timeout`` or
    ``not_running_after_ready``.
    """
    ok: bool
    reason: str
    identity: HubIdentity | None = None


def _wait_running(layout: HubLayout, timeout: float) -> HubIdentity:
    deadline = time.monotonic() + timeout
    while True:
        identity = inspect_hub(layout, timeout=max(0.5, min(2.0, deadline - time.monotonic())))
        if identity.state == RUNNING or time.monotonic() >= deadline:
            return identity
        time.sleep(0.05)


def ensure_hub(layout: HubLayout | None = None, *, timeout: float = READY_TIMEOUT) -> HubStart:
    """Use the running Hub or spawn one; each wait is bounded by ``timeout``.

    ``unknown`` (lock held, identity not yet proven, e.g. a Hub before its record)
    is waited out and never raced with a second spawn.
    """
    layout = hub_layout() if layout is None else layout
    identity = inspect_hub(layout)
    if identity.state == UNKNOWN:
        identity = _wait_running(layout, timeout)
        if identity.state == UNKNOWN:
            return HubStart(False, "hub_unknown", identity)
    if identity.state == RUNNING:
        return HubStart(True, "already_running", identity)
    ancestor = managed_ancestor(os.getpid())
    if ancestor is UNVERIFIABLE:
        return HubStart(False, PROC_UNREADABLE)
    if ancestor is not None:
        return HubStart(False, SPAWN_MANAGED)
    outcome = spawn_hub(layout, timeout=timeout)
    if outcome not in (SPAWN_STARTED, SPAWN_LOST):
        return HubStart(False, outcome)
    identity = _wait_running(layout, timeout)
    if identity.state != RUNNING:
        return HubStart(False, "not_running_after_ready", identity)
    return HubStart(True, "started" if outcome == SPAWN_STARTED else "joined", identity)
