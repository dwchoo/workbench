"""Run a test command with TMPDIR on tmpfs (/dev/shm) when it has room.

Disk fsync on this host is 20-70 ms versus ~0 ms on /dev/shm, so Workbench
test commands run noticeably faster with a tmpfs TMPDIR. Each run gets a private
``wb-tests.*`` root that is always removed afterwards; ``--clean-stale`` reaps
roots left behind by killed runs. Nothing outside ``wb-tests.*`` roots is touched.

Exit code is the command's (128+N when killed by signal N; SIGTERM/SIGHUP to this
tool are forwarded to the command), or 125 if no root could be created.
SIGTERM/SIGHUP are forwarded only to the direct child, not its process group
or descendants. A SIGTERM/SIGHUP that arrives in the brief window right after
the child is spawned (before the forwarding handlers are installed) is not
forwarded and can leave the root behind; recover it with ``--clean-stale``.
Caution: ``--ignore-unreadable`` can delete a stale root still used by a same-uid
process whose /proc entries are unreadable (only roots older than --older-than-hours).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Sequence

GIB = 1024 ** 3
SHM_DIR = "/dev/shm"
TMP_DIR = "/tmp"
PREFIX = "wb-tests."
DEFAULT_MIN_FREE_GB = 8.0
DEFAULT_STALE_HOURS = 24.0
BASE_ERROR_EXIT = 125  # wrapper could not set up the root; the command never ran


def _free_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def _warn(message: str) -> None:
    print(f"shm_tests: warning: {message}", file=sys.stderr)


def choose_base(*, shm_dir: str, tmp_dir: str, min_free_bytes: int,
                free_fn: Callable[[str], int]) -> str:
    try:
        free = free_fn(shm_dir)
    except OSError as exc:
        _warn(f"{shm_dir} unavailable ({exc.strerror or exc}); falling back to {tmp_dir}")
        return tmp_dir
    if free >= min_free_bytes:
        return shm_dir
    _warn(f"{shm_dir} has {free / GIB:.1f} GiB free < {min_free_bytes / GIB:.1f} GiB; "
          f"falling back to {tmp_dir}")
    return tmp_dir


def _min_free_gb(arg: float | None) -> float:
    if arg is not None:
        return arg
    raw = os.environ.get("WB_TEST_SHM_MIN_GB")
    if raw:
        try:
            return float(raw)
        except ValueError:
            _warn(f"ignoring invalid WB_TEST_SHM_MIN_GB={raw!r}")
    return DEFAULT_MIN_FREE_GB


def tree_size(path: str) -> int:
    """Allocated bytes under ``path`` (symlinks are counted, never followed)."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            total += os.lstat(current).st_blocks * 512
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                continue
            with os.scandir(current) as entries:
                stack.extend(entry.path for entry in entries)
        except OSError:
            continue
    return total


def _fmt(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}MiB"


class PeakSampler(threading.Thread):
    """Polls the root size so the report can show the peak, not just the end."""

    def __init__(self, root: str, interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.root, self.interval = root, interval
        self.peak = 0
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            self.peak = max(self.peak, tree_size(self.root))

    def finish(self) -> None:
        self._stop_event.set()
        self.join()


def remove_root(root: str) -> None:
    """Remove one ``wb-tests.*`` root, making read-only subtrees writable first."""
    if not os.path.basename(root).startswith(PREFIX) or os.path.islink(root):
        raise ValueError(f"refusing to remove non-root path: {root}")

    attempted: set[str] = set()

    def fix_and_retry(func, path, exc_info):
        parent = os.path.dirname(path)
        # Only ever chmod inside the root: its parent is the (possibly user-owned) base.
        for target in (path, parent):
            if not _inside(target, root):
                continue
            try:
                if not os.path.islink(target):
                    os.chmod(target, 0o700)
            except OSError:
                pass
        if func in (os.open, os.scandir, os.lstat, os.stat, os.listdir):
            # The failed call was opening/listing a directory, not a removal: rerun the
            # removal of that subtree now that it is accessible (once per path).
            if path not in attempted:
                attempted.add(path)
                _rmtree(path)
            return
        try:
            func(path)
        except FileNotFoundError:
            pass

    def _rmtree(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=lambda f, p, e: fix_and_retry(f, p, e))
        else:  # pragma: no cover
            shutil.rmtree(path, onerror=fix_and_retry)

    _rmtree(root)
    if os.path.lexists(root):
        shutil.rmtree(root, ignore_errors=True)
    if os.path.lexists(root):
        raise OSError(f"could not fully remove {root}")


def run_command(command: Sequence[str], root: str) -> int:
    env = dict(os.environ, TMPDIR=root, TEMP=root, TMP=root)
    try:
        proc = subprocess.Popen(list(command), env=env)
    except FileNotFoundError as exc:
        print(f"shm_tests: {exc}", file=sys.stderr)
        return 127
    except PermissionError as exc:
        print(f"shm_tests: {exc}", file=sys.stderr)
        return 126

    def forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except (ProcessLookupError, OSError):
            pass

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, forward)
        except ValueError:  # not the main thread
            break
    try:
        while True:
            try:
                code = proc.wait()
                break
            except KeyboardInterrupt:
                # The child shares our process group and got SIGINT itself; wait for it.
                continue
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 128 - code if code < 0 else code


def run(args: argparse.Namespace, *, shm_dir: str, tmp_dir: str,
        free_fn: Callable[[str], int]) -> int:
    base = args.base or choose_base(
        shm_dir=shm_dir, tmp_dir=tmp_dir,
        min_free_bytes=int(_min_free_gb(args.min_free_gb) * GIB), free_fn=free_fn)
    try:
        root = tempfile.mkdtemp(prefix=PREFIX, dir=base)
    except OSError as exc:
        if args.base or base == tmp_dir:
            print(f"shm_tests: cannot create a root under {base}: {exc.strerror or exc}",
                  file=sys.stderr)
            return BASE_ERROR_EXIT
        _warn(f"cannot create a root under {base} ({exc.strerror or exc}); "
              f"falling back to {tmp_dir}")
        base = tmp_dir
        try:
            root = tempfile.mkdtemp(prefix=PREFIX, dir=base)
        except OSError as exc2:
            print(f"shm_tests: cannot create a root under {base}: {exc2.strerror or exc2}",
                  file=sys.stderr)
            return BASE_ERROR_EXIT
    started = time.monotonic()
    sampler = PeakSampler(root)
    sampler.start()
    code = 1
    try:
        code = run_command(args.command, root)
    finally:
        sampler.finish()
        final = tree_size(root)
        print(f"shm_tests: base={base} root={root} size={_fmt(final)} "
              f"peak={_fmt(max(sampler.peak, final))} "
              f"elapsed={time.monotonic() - started:.1f}s", file=sys.stderr)
        if args.keep:
            print(f"shm_tests: kept {root}", file=sys.stderr)
        else:
            try:
                remove_root(root)
            except Exception as exc:  # never lose the command's exit code
                print(f"shm_tests: failed to remove {root}: {exc!r}", file=sys.stderr)
    return code


def paths_in_use(proc_dir: str = "/proc") -> tuple[set[str], list[str]]:
    """Return (cwd/open-fd targets of readable processes, unreadable pids).

    Only processes owned by our uid are examined (judged by the owner of
    ``/proc/<pid>``): other users cannot enter our 0700 roots. A pid is reported
    as unreadable when listing its ``fd`` directory or reading one of its links
    raises PermissionError. Known gap: a same-uid process that is non-dumpable
    gets root-owned ``/proc/<pid>`` entries, so the owner check skips it
    silently; it is neither examined nor counted as unreadable, and a root only
    it uses can be treated as unused.
    """
    used: set[str] = set()
    unreadable: list[str] = []
    uid = os.getuid()
    for entry in os.listdir(proc_dir):
        if not entry.isdigit():
            continue
        base = os.path.join(proc_dir, entry)
        try:
            if os.stat(base).st_uid != uid:
                continue
            links = [os.path.join(base, "cwd")]
            links += [os.path.join(base, "fd", fd) for fd in os.listdir(os.path.join(base, "fd"))]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            unreadable.append(entry)
            continue
        blocked = False
        for link in links:
            try:
                used.add(os.readlink(link))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                blocked = True
            except OSError:
                continue
        if blocked:
            unreadable.append(entry)
    return used, unreadable


def _inside(target: str, root: str) -> bool:
    target = target.removesuffix(" (deleted)")
    return target == root or target.startswith(root + os.sep)


def find_stale(bases: Sequence[str], older_than_hours: float, *, proc_dir: str = "/proc",
               now: float | None = None, ignore_unreadable: bool = False) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
    """Return (stale roots with sizes, skipped roots with reasons)."""
    now = time.time() if now is None else now
    cutoff = now - older_than_hours * 3600
    uid = os.getuid()
    stale: list[tuple[str, int]] = []
    skipped: list[tuple[str, str]] = []
    used: set[str] | None = None
    unverifiable = ""
    seen: set[str] = set()
    for base in bases:
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if not name.startswith(PREFIX):
                continue
            root = os.path.join(os.path.realpath(base), name)
            if root in seen:
                continue
            seen.add(root)
            try:
                info = os.lstat(root)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                skipped.append((root, "not a plain directory"))
                continue
            if info.st_uid != uid:
                skipped.append((root, "owned by another user"))
                continue
            if info.st_mtime >= cutoff:
                continue
            if used is None and not unverifiable:
                used, unreadable = paths_in_use(proc_dir)
                if unreadable and not ignore_unreadable:
                    unverifiable = (f"/proc denies pid(s) {','.join(unreadable[:5])}"
                                    f"{'...' if len(unreadable) > 5 else ''}; "
                                    "--ignore-unreadable to accept")
            if unverifiable:
                skipped.append((root, f"cannot verify it is unused ({unverifiable})"))
            elif any(_inside(target, root) for target in used or ()):
                skipped.append((root, "in use by a running process"))
            else:
                stale.append((root, tree_size(root)))
    return stale, skipped


def clean_stale(args: argparse.Namespace, *, shm_dir: str, tmp_dir: str) -> int:
    bases = [args.base] if args.base else [shm_dir, tmp_dir]
    stale, skipped = find_stale(bases, args.older_than_hours,
                                 ignore_unreadable=args.ignore_unreadable)
    verb = "removing" if args.apply else "would remove"
    for root, size in stale:
        print(f"{verb} {root} ({_fmt(size)})")
    for root, reason in skipped:
        print(f"skipped {root}: {reason}")
    failed = 0
    if args.apply:
        for root, _size in stale:
            try:
                remove_root(root)
            except Exception as exc:  # one bad root must not abort the rest
                failed += 1
                print(f"failed {root}: {exc}", file=sys.stderr)
    total = sum(size for _root, size in stale)
    print(f"{len(stale)} stale root(s), {_fmt(total)}"
          + ("" if args.apply else " (dry-run; pass --apply to delete)"))
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="%(prog)s [options] -- COMMAND [ARG ...]")
    parser.add_argument("--base", help="base directory for the per-run root "
                        "(default: /dev/shm if enough room, else /tmp)")
    parser.add_argument("--min-free-gb", type=float, default=None,
                        help=f"minimum free GiB on /dev/shm (default {DEFAULT_MIN_FREE_GB:g}, "
                        "env WB_TEST_SHM_MIN_GB)")
    parser.add_argument("--keep", action="store_true", help="keep the root and print its path")
    parser.add_argument("--clean-stale", action="store_true",
                        help="list (dry-run) or remove stale unused wb-tests.* roots")
    parser.add_argument("--older-than-hours", type=float, default=DEFAULT_STALE_HOURS)
    parser.add_argument("--ignore-unreadable", action="store_true",
                        help="with --clean-stale: do not let same-user processes whose /proc "
                        "links are unreadable (non-dumpable daemons) block cleanup. Risk: this can "
                        "delete a stale root still used by a same-uid process whose /proc entries "
                        "are unreadable (only roots older than --older-than-hours)")
    parser.add_argument("--apply", action="store_true", help="with --clean-stale: delete")
    parser.add_argument("command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None, *, shm_dir: str = SHM_DIR, tmp_dir: str = TMP_DIR,
         free_fn: Callable[[str], int] = _free_bytes) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.clean_stale and not args.command:
        parser.error("missing command after --")
    if args.clean_stale:
        return clean_stale(args, shm_dir=shm_dir, tmp_dir=tmp_dir)
    return run(args, shm_dir=shm_dir, tmp_dir=tmp_dir, free_fn=free_fn)


if __name__ == "__main__":
    raise SystemExit(main())
