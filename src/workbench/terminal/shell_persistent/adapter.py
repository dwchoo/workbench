"""Persistent shell adapter over the verified G2 transport, not a Task runner.

Callers supply the user environment and validated approval/automation ports.
This adapter never approves a Task, infers business success, or replays a request.
Linux Bash/sh only; unsupported exec tracing remains UNKNOWN in G2.
"""
from __future__ import annotations

from dataclasses import asdict
import fcntl
import os
from pathlib import Path
import select
import shlex
import termios
import time

from workbench.contracts.ports_v2 import dispatch_allowed, parse_port
from workbench.terminal.shell_g2.control_probe import ForegroundTarget
from workbench.terminal.shell_g2.lifecycle import ManagedLifecycleProbe, managed_controller_source
from workbench.terminal.shell_g2.prototype import ShellChoice, UnsafeShellState, select_shell


# The builtins the persistent Bash controller and its init call (HSB review fix 4: ``enable -n`` of one of them
# drops the rc).
_CONTROLLER_BUILTINS = frozenset({"printf", "echo", "test", "[", "trap", "read", "eval", "jobs", "compgen", "shopt",
                                  "set", "unset", "declare", "cd", "enable", "command", "return", "continue",
                                  "alias", ":", "builtin"})
# HSB-D02: after the user's rc, what Workbench's own controller needs is put back before it is defined. Every
# command is a ``builtin`` (review fix 2): a user function or alias named trap/set/unset/shopt/... cannot defeat
# it. A function named ``builtin`` itself, and builtins the rc disabled with ``enable -n``, are recorded first
# (then undone so the init can finish and report them; HSB-D05 drops such an rc).
# DEBUG/ERR/RETURN traps would run the user's code inside the controller and a CHLD trap is a rejected hook
# (C-D41); other traps (EXIT, INT at the prompt, ...) stay. Strict/tracing options would abort or echo the
# controller's functions, noclobber (-C) is reset too (the controller also writes with ``>|``), and allexport
# (-a) stays OFF so the controller's variables are never exported (review fix 3). PROMPT_COMMAND may be an array
# (bash 5.1+): unset first so only Workbench's hook remains. The controller's functions are defined without the
# user's aliases (``alias printf=...`` would rewrite their bodies); alias expansion is on again afterwards.
_USER_RC_RESET = (
    "__b_shadow=$(\\declare -F builtin 2>/dev/null) || __b_shadow=\n"  # never a failure under the rc's set -e
    "\\unset -f builtin 2>/dev/null\n"
    "\\builtin trap - DEBUG RETURN ERR CHLD\n"
    "__b_disabled=$(\\builtin enable -n)\n"
    f"\\builtin enable {' '.join(sorted(_CONTROLLER_BUILTINS))} 2>/dev/null\n"
    "\\builtin trap - DEBUG RETURN ERR CHLD\n"
    "\\builtin set +aeuxvETC +o pipefail\n"
    "\\builtin shopt -u extdebug expand_aliases\n"
    "builtin unset PROMPT_COMMAND\n")
_USER_RC_AFTER = "builtin shopt -s expand_aliases\n"
# HSB-D07: the host shell starts where it was launched (the project dir) whatever the rc ``cd``ed to, with the
# OLDPWD from before the rc. A start dir that cannot be entered again keeps the rc's dir and is reported.
_USER_RC_BEFORE = "__b_start_dir=$PWD __b_oldpwd_set=${OLDPWD+x} __b_oldpwd=${OLDPWD-}\n"
_USER_RC_CWD = """__b_cwd_note=
if builtin test -z "$__b_start_dir"; then
    __b_cwd_note=1  # bash found no start directory (deleted before the shell started)
elif builtin test "$PWD" != "$__b_start_dir"; then
    if builtin cd -- "$__b_start_dir" 2>/dev/null; then
        if builtin test -n "$__b_oldpwd_set"; then OLDPWD=$__b_oldpwd; else builtin unset OLDPWD; fi
    else
        __b_cwd_note=1
    fi
fi
builtin unset __b_start_dir __b_oldpwd_set __b_oldpwd
"""
# HSB-D05: what the rc may have left that the reset cannot undo, written (``builtin`` only) before the first
# prompt: ``ps1`` / ``prompt_command`` (readonly), ``function:builtin``, ``enable -n <name>`` (disabled builtins)
# and ``note:cwd`` (HSB-D07); ``end`` closes a complete report.
_USER_RC_CHECK = """{{
builtin test "$PS1" = '$ ' || builtin echo ps1
builtin test "${{#PROMPT_COMMAND[@]}}" = 1 && builtin test "$PROMPT_COMMAND" = '__b_emit READY' || builtin echo prompt_command
builtin test -z "$__b_shadow" || builtin echo function:builtin
builtin test -z "$__b_disabled" || builtin printf '%s\\n' "$__b_disabled"
builtin test -z "$__b_cwd_note" || builtin echo note:cwd
builtin echo end
}} >| {status}
builtin unset __b_cwd_note __b_shadow __b_disabled
"""
# HSB-D05: how long the rc may take before the host shell is started again without it.
USER_RC_SECONDS = 10.0


class _Transport(ManagedLifecycleProbe):
    def __init__(self, choice, environment, *, user_rc=None, host_bash=False):
        # HSB-D01: a Bash host shell exports WORKBENCH_SHELL=1 and sources ``user_rc`` (the readable regular
        # ``~/.bashrc``, or None) before its own init.
        self._host_bash = host_bash and choice.kind == "bash"
        self._user_rc = user_rc if self._host_bash else None
        # dash reads the init file through ENV once at startup; the user's own
        # value (or its absence) is restored so children never inherit ours.
        self._user_env_value = environment.get("ENV")
        self.takeover_requested = False
        self.sent_id = None
        self.confirmed_target = None
        self._child_start = None
        self._parent_start = None
        # Count of this parent's own HANDOFF events: one control wait episode each.
        self.handoff_episode = 0
        super().__init__(choice, environment=environment)

    @property
    def rc_status_path(self) -> Path:
        """HSB-D05: the init's report on what the user's rc left behind (see ``_USER_RC_CHECK``)."""
        return Path(self._init_dir.name) / "rc-status"

    @property
    def env_names_path(self) -> Path:
        """CW-16 fix-02: where the prompt hook writes the exported names (never values) at each prompt."""
        return Path(self._init_dir.name) / "env-names"

    def _control_init_source(self):
        # Private control paths are non-exported shell locals, not user env.
        root = Path(self._init_dir.name)
        # The hook's ``>|`` keeps an existing file's mode: create it 0600 so the user's umask never widens it.
        os.close(os.open(self.env_names_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        setup = (("builtin " if self._host_bash else "")
                 + "unset BOUNDARY_TRAPS BOUNDARY_TRAPS_AFTER BOUNDARY_PREPARED BOUNDARY_ENV_NAMES\n"
                 f"BOUNDARY_TRAPS={shlex.quote(str(root / 'before'))}\n"
                 f"BOUNDARY_TRAPS_AFTER={shlex.quote(str(root / 'after'))}\n"
                 f"BOUNDARY_ENV_NAMES={shlex.quote(str(self.env_names_path))}\n"
                 "BOUNDARY_PREPARED=persistent\n")
        if self._user_rc is not None:
            os.close(os.open(self.rc_status_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
            rc = shlex.quote(self._user_rc)
            # HSB-D01: like bash's own interactive non-login start, but the control FDs are hidden from it.
            setup = (_USER_RC_BEFORE + f"if [ -f {rc} ] && [ -r {rc} ]; then . {rc} 7<&- 8<&- 9>&-; fi\n"
                     + _USER_RC_RESET + _USER_RC_CWD) + setup
        if self._host_bash:
            setup = "export WORKBENCH_SHELL=1\n" + setup
        if self.choice.kind == "sh":
            setup += ("unset ENV\n" if self._user_env_value is None
                      else f"export ENV={shlex.quote(self._user_env_value)}\n")
        after = ("" if self._user_rc is None else
                 _USER_RC_CHECK.format(status=shlex.quote(str(self.rc_status_path))) + _USER_RC_AFTER)
        return setup + managed_controller_source(self.choice.executable, handoff_checks=True) + after

    def _on_control_event(self, event):
        if event == f"HANDOFF:{self.pid}":
            self.handoff_episode += 1
        super()._on_control_event(event)
        if event.startswith("CHILD_PREPARED:"):
            self._child_start = None  # a new child's ticks are unknown until EXEC_READY
        if event.startswith("EXEC_READY:") and not self.lifecycle.unknown:
            try:
                self._child_start = self._proc(self.lifecycle.child_pid)[19]
            except (OSError, ValueError, IndexError):
                # An already-reaped short command is a valid G2 completion.
                # A live foreground target still cannot be confirmed without
                # this identity, so input remains held closed.
                self._child_start = None
        if event.startswith(("MAIN_RETURN:", "MAIN_STOPPED:")):
            self.confirmed_target = None
        if event in {"CONTROL_LOST", "HOOK_LOST", "HOOK_REJECTED", "SHELL_EXIT"}:
            self.confirmed_target = None
            self.manual_prompt_confirmed = False
        # wb-handoff refused before its control loop (no WAIT yet) returns 1 to
        # the interactive prompt. The parent's own HOOK_REJECTED is that return
        # event: a removed prompt hook means no READY may ever follow. The
        # manual target is still revalidated on every write; automation stays
        # held (no CONTROL_READY, so no clean handoff).
        if (event == "HOOK_REJECTED" and not self.control_wait_seen
                and (self.lifecycle.request_id is None or self.lifecycle.returned)):
            self.manual_prompt_confirmed = True

    @staticmethod
    def _proc(pid):
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()

    def _current_foreground_child_group(self, *, any_owner=False):
        """The verified managed child's group while it is the PTY foreground, else None.

        ``any_owner`` skips only the input-owner requirement (CS-07 Ctrl+C); the confirmed-takeover
        path keeps requiring a user-owned boundary.
        """
        life = self.lifecycle
        if (not life.experiment_started or life.main_exit is not None or life.main_stopped
                or life.unknown or (self.boundary.owner != "user" and not any_owner)
                or self.boundary.uncertain or self.boundary.needs_review):
            return None
        try:
            fields = self._proc(life.child_pid)
            if (fields[19] == self._child_start and fields[0] not in {"X", "Z", "T", "t"}
                    and int(fields[1]) == life.supervisor_pid
                    and int(fields[3]) == self.pid and int(fields[2]) == life.child_group
                    and self._foreground_group() == life.child_group):
                return life.child_group
        except (OSError, ValueError, IndexError, TypeError):
            pass
        return None

    def _manual_prompt_is_current(self):
        """The verified parent shell itself is the manual PTY foreground."""
        return _manual_parent_verified(self) and self._foreground_group() == self.pid

    def _manual_foreground_group(self):
        """A live user-started foreground group in the parent's session, or None.

        Observed at call time (TIOCGPGRP plus /proc), never remembered: C-AC-08
        delivers manual bytes to the current target and holds an unknown one.
        """
        if not _manual_parent_verified(self):
            return None
        group = self._foreground_group()
        if group is None or group <= 0 or group == self.pid:
            return None
        # Cheap per-write proof: one live member (the leader, or the member a
        # full scan found for this same group) re-read from /proc. The full
        # session scan runs only when that proof is missing, i.e. the group
        # changed or its remembered member ended.
        cached = getattr(self, "_fg_witness", None)
        if isinstance(cached, tuple) and cached[0] == group and _live_member(cached[1], group, self.pid, cached[2]):
            return group
        member, start = group, _live_member(group, group, self.pid)
        if start is None:
            member = _live_group_member(group, self.pid)
            start = _live_member(member, group, self.pid) if member else None
        self._fg_witness = (group, member, start) if start is not None else None
        return group if start is not None else None

    def send_manual_parent(self, data):
        """Write only to the still-verified parent, with a bounded retry window."""
        _write_manual(self, data, self._manual_prompt_is_current,
                      lambda chunk: self.boundary.observe_manual_bytes(chunk, shell_foreground=True),
                      "manual prompt target changed")

    def send_manual_foreground(self, data, group):
        """Write to the observed user foreground group; the tty delivers signals."""
        _write_manual(self, data, lambda: self._manual_foreground_group() == group,
                      lambda chunk: self.boundary.observe_manual_bytes(chunk, shell_foreground=False),
                      "manual foreground target changed")

    def send_confirmed_foreground(self, data):
        """Write experiment input to the confirmed automation foreground run."""
        target = self.confirmed_target

        def current():
            return (target is not None and self.confirmed_target == target and self.takeover_requested
                    and target.command_id == self.sent_id
                    and self._current_foreground_child_group() == target.process_group)

        _write_manual(self, data, current, self.boundary.observe_run_bytes,
                      "confirmed foreground target changed")

    def _interrupt_target(self):
        """Identity of the verified managed run that is the PTY foreground now, or None.

        Re-read from the lifecycle and /proc on every call; never remembered.
        """
        life = self.lifecycle
        if self._closed or self.boundary.control_lost or life.request_id is None or life.returned:
            return None
        group = self._current_foreground_child_group(any_owner=True)
        if group is None or self._child_start is None or self._supervisor_start is None:
            return None
        return {"request_id": life.request_id, "pid": life.child_pid, "start_ticks": int(self._child_start),
                "process_group": group}

    def send_interrupt(self, target):
        """Write one ``\\x03`` to the PTY if ``target`` is still the verified foreground run.

        The byte is not input to a shell line, so no ``observe_*_bytes`` accounting happens.
        """
        _write_manual(self, b"\x03", lambda: self._interrupt_target() == target, lambda chunk: None,
                      "interrupt target changed")

    def release_idle(self):
        if self.lifecycle.request_id is not None or not self.control_wait_seen:
            raise UnsafeShellState("not an idle parent control wait")
        self._flush_takeover()

    def _flush_takeover(self):
        fd = fcntl.ioctl(self.master_fd, 0x5441, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        finally:
            os.close(fd)
        if os.write(self._request_fd, b"TAKEOVER\n") != len(b"TAKEOVER\n"):
            self.lifecycle.fail_unknown("takeover_write_failure")
            raise OSError("partial takeover; no retry")


def _user_rc_path(environment):
    """``$HOME/.bashrc`` of the shell's own environment, or None without an absolute HOME."""
    home = environment.get("HOME", "")
    return os.path.join(home, ".bashrc") if os.path.isabs(home) else None


_RC_PROBLEMS = {"ps1": "PS1을 Workbench 값으로 되돌릴 수 없음(readonly)",
                "prompt_command": "PROMPT_COMMAND를 Workbench 값으로 되돌릴 수 없음(readonly)"}


def _user_rc_problem(t, seconds, on_note=None):
    """HSB-D05: None once the rc-loading shell reached its first prompt intact, else the one-line reason.

    Bounded by ``seconds``; never types anything. The shell is observed through its own report
    (``rc-status``), its READY, and /proc (exited, or replaced by ``exec``).
    """
    deadline = time.monotonic() + seconds
    shell = os.path.realpath(t.choice.executable)
    exec_seen = False  # the forked child shows Python's exe until its own execve
    while True:
        t._drain(0.03)
        try:
            report = t.rc_status_path.read_text("utf-8", "replace").splitlines()
        except OSError:
            report = []
        if report[-1:] == ["end"]:
            problems = [_RC_PROBLEMS[item] for item in report if item in _RC_PROBLEMS]
            names = [item.removeprefix("function:") for item in report if item.startswith("function:")]
            if names:
                problems.append(f"Workbench가 쓰는 명령을 함수로 재정의함({', '.join(names)})")
            disabled = [item for item in report if item.startswith("enable -n ")
                        and item.removeprefix("enable -n ") in _CONTROLLER_BUILTINS]
            if disabled:
                problems.append(f"Workbench가 쓰는 builtin을 끔({', '.join(disabled)})")
            if problems:
                return "; ".join(problems)
            if "READY" in t.events:
                if "note:cwd" in report and on_note is not None:
                    on_note("시작 디렉터리로 돌아갈 수 없어(삭제됨 등) 셸의 현재 디렉터리를 유지함")
                return None
        try:
            state = t._proc(t.pid)[0]
            exe = os.path.realpath(os.readlink(f"/proc/{t.pid}/exe"))
        except (OSError, IndexError):
            state, exe = "Z", None
        if state in {"Z", "X"}:
            return "셸이 ~/.bashrc 안에서 종료됨(exit 등)"
        if exe == shell:
            exec_seen = True
        elif exec_seen and exe is not None:
            return f"셸이 다른 프로그램({os.path.basename(exe)})으로 exec됨"
        if time.monotonic() >= deadline:
            return f"{seconds:g}초 안에 끝나지 않음(입력 대기·느린 초기화 등)"


def _manual_parent_verified(t):
    """User-owned interactive parent (not control wait/in-flight), identity intact.

    Line-edit uncertainty and an earlier needs_review latch deliberately do not
    appear here: they hold automation (can_dispatch/claim_manager), not the
    user's input. A lost control FD does, since the parent is unobservable.
    """
    life, boundary = t.lifecycle, t.boundary
    if (t._closed or not t.manual_prompt_confirmed or boundary.owner != "user"
            or boundary.control_lost or life.unknown
            or (life.request_id is not None and not life.returned)):
        return False
    try:
        fields = t._proc(t.pid)
        return (fields[19] == t._parent_start and fields[0] not in {"X", "Z", "T", "t"}
                and int(fields[1]) == os.getpid() and int(fields[2]) == t.pid
                and int(fields[3]) == t.pid)
    except (OSError, ValueError, IndexError, TypeError):
        return False


def _live_member(pid, group, session, start=None):
    """Start ticks if pid is a running (not zombie/stopped) member of group in
    session (and, when given, still the same process); otherwise None."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        if (int(fields[2]) == group and int(fields[3]) == session
                and fields[0] not in {"X", "Z", "T", "t"}
                and (start is None or fields[19] == start)):
            return fields[19]
    except (OSError, ValueError, IndexError):
        pass
    return None


def _live_group_member(group, session):
    """Full /proc scan: a running member of group in session, or None."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if name.isdecimal() and _live_member(int(name), group, session) is not None:
            return int(name)
    return None


def _write_manual(t, data, still_current, observe, changed):
    """Bounded manual PTY write; the target is revalidated before every attempt."""
    with t.boundary.lock:
        view = memoryview(data)
        offset = 0
        deadline = time.monotonic() + 0.25
        # A latch set before this write does not hold the user; a fail-closed
        # boundary loss during it withholds every remaining byte.
        latched = t.boundary.needs_review
        try:
            while offset < len(view):
                if time.monotonic() >= deadline:
                    t.boundary.fail_closed()
                    raise TimeoutError("partial manual PTY write; remaining bytes withheld")
                try:
                    t._drain(0)
                except OSError as exc:
                    t.lifecycle.fail_unknown("control_observation_lost")
                    t.boundary.fail_closed()
                    raise UnsafeShellState("manual input boundary unavailable") from exc
                if (t.boundary.needs_review and not latched) or not still_current():
                    if offset:
                        t.boundary.fail_closed()
                    raise UnsafeShellState(changed)
                try:
                    written = os.write(t.master_fd, view[offset:])
                except (InterruptedError, BlockingIOError):
                    written = 0
                except OSError:
                    t.boundary.fail_closed()
                    raise
                if written:
                    observe(bytes(view[offset:offset + written]))
                    offset += written
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    t.boundary.fail_closed()
                    raise TimeoutError("partial manual PTY write; remaining bytes withheld")
                try:
                    ready = select.select([], [t.master_fd], [], remaining)[1]
                except InterruptedError:
                    continue
                except OSError:
                    t.boundary.fail_closed()
                    raise
                if not ready:
                    t.boundary.fail_closed()
                    raise TimeoutError("partial manual PTY write; remaining bytes withheld")
        finally:
            view.release()


class PersistentShell:
    """One serialized writer, one outstanding request, explicit takeover state."""
    def __init__(self, *, user_environment: dict[str, str], choice: ShellChoice | None = None,
                 load_user_rc: bool = False):
        if not isinstance(user_environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in user_environment.items()):
            raise ValueError("explicit string user environment required")
        selected = choice or select_shell(user_environment.get("PATH", ""))
        if selected.kind not in {"bash", "sh"} or Path(os.path.realpath(selected.executable)).name not in {"bash", "sh", "dash"}:
            raise ValueError("only verified Bash/sh supported")
        # HSB: {"state": "applied" | "absent" | "fallback", "path", "reason"} for a Bash host shell, else None.
        self.user_rc = None
        rc = None
        if load_user_rc and selected.kind == "bash":
            path = _user_rc_path(user_environment)
            rc = path if path is not None and os.path.isfile(path) and os.access(path, os.R_OK) else None
            self.user_rc = {"state": "applied" if rc else "absent", "path": path, "reason": None}
        self._transport = _Transport(selected, dict(user_environment), user_rc=rc,
                                     host_bash=self.user_rc is not None)
        self._seen = set()
        self._takeover_sent = False
        self._takeover_episode = None
        self._approval_hash = None
        # HSB-D05: bytes shown once before the shell's own output (the reason an rc was dropped).
        self._notice = b""
        try:
            if rc is not None:
                reason = _user_rc_problem(self._transport, USER_RC_SECONDS,
                                          on_note=lambda note: self.user_rc.update(reason=note))
                if reason is not None:
                    self._transport.close()
                    self.user_rc = {"state": "fallback", "path": rc, "reason": reason}
                    self._notice = f"\r\n[Workbench] ~/.bashrc 적용 실패: {reason} — 기본 설정으로 시작\r\n".encode()
                    self._transport = _Transport(selected, dict(user_environment), host_bash=True)
            self._transport.wait_ready()
            try:
                self._transport._parent_start = self._transport._proc(self._transport.pid)[19]
            except (OSError, ValueError, IndexError) as exc:
                raise UnsafeShellState("parent shell identity unconfirmed") from exc
            self._transport.take_user_control()
            self._transport.manual_prompt_confirmed = True
        except BaseException:
            self.close()
            raise

    @property
    def parent_pid(self):
        return self._transport.pid

    def poll(self, timeout=0):
        t = self._transport
        with t.boundary.lock:
            t._drain(timeout)
            if t.confirmed_target is not None and t._current_foreground_child_group() != t.confirmed_target.process_group:
                t.confirmed_target = None
            if t.takeover_requested and not self._takeover_sent and t.control_wait_seen and (
                    t.lifecycle.request_id is None or t.lifecycle.returned):
                self._takeover_sent = True  # A failed send is never retried.
                self._takeover_episode = t.handoff_episode
                try:
                    t.release_idle() if t.lifecycle.request_id is None else t.release_control()
                except OSError:
                    t.lifecycle.fail_unknown("takeover_write_failure")
                    t.boundary.fail_closed()
                    raise
            return self.snapshot()

    def run_identity(self):
        """Read-only identity of the outstanding managed request, or None without one.

        Process refs are ``{"pid", "start_ticks"}`` from the lifecycle events and the start
        ticks this adapter recorded (None when unconfirmed or already reaped) and ``main_exit``
        (``{"exit_code", "signal"}`` once the main process's end was observed, else None). The numbers
        are a claim to re-verify through a pin before any signal, never a license to signal.
        """
        t = self._transport
        with t.boundary.lock:
            life = t.lifecycle
            if life.request_id is None:
                return None

            def ref(pid, start):
                return {"pid": pid, "start_ticks": int(start) if start is not None else None} if pid else None
            try:
                boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
            except OSError:
                boot_id = None
            phase = ("ended" if life.lifetime == "ended" else "unknown" if life.unknown
                     else "running" if life.experiment_started else "starting")
            # how the main process ended, once observed (a negative lifecycle value is "killed by signal -n")
            main_exit = (None if life.main_exit is None else
                         {"exit_code": 128 - life.main_exit, "signal": -life.main_exit} if life.main_exit < 0
                         else {"exit_code": life.main_exit, "signal": None})
            return {"request_id": life.request_id, "phase": phase,
                    "parent": ref(t.pid, t._parent_start),
                    "supervisor": ref(life.supervisor_pid, t._supervisor_start),
                    "child": ref(life.child_pid, t._child_start),
                    "child_group": life.child_group, "main_exit": main_exit,
                    "session": t.pid, "uid": os.geteuid(), "boot_id": boot_id}

    def interrupt_run(self):
        """Deliver a user Ctrl+C (``\\x03``) to the verified foreground managed run.

        Returns the verified identity (``request_id``, ``pid``, ``start_ticks``, ``process_group``,
        plus ``session``/``uid``) once the byte is written, or None when no managed run
        is a verified foreground right now (the caller then applies its ordinary input rules).
        The target is re-verified immediately before the write; owner and takeover state are not
        read or changed.
        """
        t = self._transport
        with t.boundary.lock:
            try:
                target = t._interrupt_target()
                if target is None:
                    return None
                t.send_interrupt(target)
            except (UnsafeShellState, OSError, TimeoutError):
                return None
            return {**target, "session": t.pid, "uid": os.geteuid()}

    def snapshot(self):
        t, life = self._transport, self._transport.lifecycle
        phase = ("unknown" if life.unknown or t.boundary.uncertain or t.boundary.needs_review else "control_returned" if life.control_returned else
                 "input_returned" if life.input_returned else "lifetime_ended" if life.lifetime == "ended" else
                 "main_returned" if life.main_exit is not None else "experiment_started" if life.experiment_started else
                 "supervisor_started" if life.supervisor_pid else "accepted" if life.accepted else "not_sent")
        reasons = []
        if t.takeover_requested: reasons.append("takeover_requested")
        if t.boundary.owner != "manager": reasons.append("user_owner")
        if not t.control_wait_seen: reasons.append("parent_not_control_wait")
        if t.boundary.pending_line or t.boundary.submitted_lines: reasons.append("unsubmitted_or_unconsumed_input")
        if t.boundary._job_pids: reasons.append("manual_jobs")
        if t.boundary.uncertain or t.boundary.needs_review or life.unknown: reasons.append("unknown_or_manual_residue")
        if "HOOK_REJECTED" in t.events: reasons.append("unsupported_hook_or_trap")
        if life.request_id is not None: reasons.append("outstanding_request")
        foreground = t._foreground_group()
        mode = ("control_wait" if t.control_wait_seen and not t.manual_prompt_confirmed else
                "manual_foreground" if t.manual_prompt_confirmed and foreground != t.pid else
                "manual_prompt" if t.manual_prompt_confirmed and t.boundary.ready and not t.boundary.submitted_lines and not t.boundary.pending_line else
                "manual_input" if t.manual_prompt_confirmed else "unknown")
        return {"parent_pid": t.pid, "generation": t.generation, "input_owner": t.boundary.owner,
                "owner_epoch": t.owner_epoch, "parent_mode": mode, "foreground_group": foreground,
                "takeover_requested": t.takeover_requested, "takeover_confirmed": t.confirmed_target is not None or (t.takeover_requested and mode == "manual_prompt" and phase != "unknown"),
                "foreground_target": asdict(t.confirmed_target) if t.confirmed_target else None,
                "phase": phase, "lifecycle": asdict(life), "held_reasons": reasons, "task_success": None}

    def request_takeover(self):
        t = self._transport
        with t.boundary.lock:
            self.poll()
            if t.lifecycle.request_id is None and t.boundary.owner == "user" and t.manual_prompt_confirmed:
                return self.snapshot()
            if (self._takeover_sent and t.boundary.owner == "user" and t.control_wait_seen
                    and not t.manual_prompt_confirmed
                    and (t.lifecycle.request_id is None or t.lifecycle.returned)
                    and t.handoff_episode != getattr(self, "_takeover_episode", None)):
                # C-D58: a held wb-handoff left the user-owned parent in a newer
                # idle control wait than the one the earlier TAKEOVER ended. This
                # explicit request gives that prompt back (one TAKEOVER per
                # episode); automation stays held by the boundary.
                self._takeover_sent = False
            t.takeover_requested = True
            t.take_user_control()
            self.poll()
            return self.snapshot()

    def confirm_takeover(self):
        t = self._transport
        with t.boundary.lock:
            self.poll()
            if not t.takeover_requested:
                raise UnsafeShellState("no takeover request")
            state = self.snapshot()
            if state["parent_mode"] == "manual_prompt":
                return state
            group = t._current_foreground_child_group()
            if group is None or t.sent_id is None:
                if self._takeover_sent and state["parent_mode"] == "control_wait":
                    raise UnsafeShellState("takeover sent; parent prompt not yet returned")
                raise UnsafeShellState("foreground target unknown; input held")
            t.confirmed_target = ForegroundTarget(t.sent_id, group)
            return self.snapshot()

    def manual_input_hold(self):
        """None if manual bytes have a current target now, else the visible reason."""
        t = self._transport
        with t.boundary.lock:
            life = t.lifecycle
            if t.boundary.owner != "user":
                return "manager owns shell input; request takeover first"
            if t.confirmed_target is not None or t._manual_prompt_is_current():
                return None
            if t._manual_foreground_group() is not None:
                return None
            if t._closed:
                return "shell closed"
            if t.boundary.control_lost:
                return "shell control channel lost"
            if life.unknown:
                return "shell lifecycle unknown: " + ", ".join(life.unknown)
            if life.request_id is not None and not life.returned:
                return ("managed request in flight; confirm takeover of its foreground target"
                        if t.takeover_requested else "managed request in flight")
            if not t.manual_prompt_confirmed:
                return ("parent in control wait; request takeover" if t.control_wait_seen else
                        "parent shell prompt not confirmed (control/hook lost or handoff pending)")
            return "foreground process group unobservable or outside the shell session"

    def send_user(self, data: bytes):
        t = self._transport
        with t.boundary.lock:
            try:
                self.poll()
            except OSError as exc:
                t.lifecycle.fail_unknown("control_observation_lost")
                t.boundary.fail_closed()
                raise UnsafeShellState("manual input boundary unavailable") from exc
            if t.boundary.owner != "user":
                raise UnsafeShellState("manager owns shell input")
            if t.confirmed_target is not None:
                t.send_confirmed_foreground(data)
                return
            life = t.lifecycle
            if (life.unknown or not t.manual_prompt_confirmed or t.boundary.control_lost
                    or (life.request_id is not None and not life.returned)):
                raise UnsafeShellState("no confirmed manual input target")
            if t._manual_prompt_is_current():
                t.send_manual_parent(data)
                return
            group = t._manual_foreground_group()
            if group is None:
                raise UnsafeShellState("no confirmed manual input target")
            t.send_manual_foreground(data, group)

    def claim_manager(self):
        t = self._transport
        with t.boundary.lock:
            self.poll()
            if not t.control_wait_seen or not t.boundary._handoff_ready or t.boundary.uncertain or t.boundary.needs_review:
                raise UnsafeShellState("no clean direct same-parent handoff")
            if t.lifecycle.request_id is not None:
                t.rearm_after_handoff()
                t._child_start = None  # per-request start ticks never outlive their request
            t.return_to_manager()
            t.takeover_requested = False
            t.confirmed_target = None
            t.sent_id = None
            self._takeover_sent = False
            return self.snapshot()

    def submit(self, control: dict, command: str | list[str], automation: dict, *, start_timeout=5.0,
               return_timeout=None):
        """Send one approved request. ``start_timeout`` bounds only the launch
        handshake; the experiment itself has no runtime limit (``return_timeout``
        is accepted for legacy callers and never bounds the run)."""
        value = parse_port(control)
        if value["kind"] != "ShellControl" or value["payload"]["phase"] != "accepted" or not dispatch_allowed(automation):
            raise UnsafeShellState("approval/automation port held")
        p, t = value["payload"], self._transport
        with t.boundary.lock:
            self.poll()
            if (t.takeover_requested or p["requestId"] in self._seen or p["parentPid"] != t.pid
                    or not t.boundary.can_dispatch(generation=p["generation"], owner_epoch=p["ownerEpoch"])
                    or t.lifecycle.request_id is not None):
                raise UnsafeShellState("stale, outstanding, unknown or dirty shell; no replay")
            # Use literal argv even for scripts; do not expose fixture directives.
            argv = [t.choice.executable, "-c", command] if isinstance(command, str) else command
            self._seen.add(p["requestId"])
            t.sent_id = p["requestId"]
            self._approval_hash = p["approvalHash"]
            t.dispatch_managed(p["requestId"], argv, start_timeout=start_timeout, return_timeout=return_timeout)
            return self.snapshot()

    def release_input(self):
        self._transport.release_input()

    def display_bytes(self):
        notice, self._notice = self._notice, b""
        return notice + self._transport.display_bytes()

    def close(self):
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
