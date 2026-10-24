"""Subreaper test fixture (HB-03, seam S9): run a command, then reap every orphan.

argv: ``<shape> <report.json> <command...>``. ``<shape>`` is only part of the argv
shape (``--supervisor`` when exec'd through ``shell_g2/lifecycle.py``, else ``--plain``).
The fixture sets ``PR_SET_CHILD_SUBREAPER``, runs the command as its child and, like the
WB shell supervisor, waits until no descendant is left. The report is
``{"main": <exit code>, "reaped": {"<pid>": <exit code>}}`` (orphans it reaped).
"""
from __future__ import annotations

import ctypes
import json
import os
import sys

PR_SET_CHILD_SUBREAPER = 36


def main(argv: list[str]) -> int:
    _shape, report, command = argv[0], argv[1], argv[2:]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER)")
    child = os.fork()
    if child == 0:  # pragma: no cover - exec'd immediately
        try:
            os.execvp(command[0], command)
        finally:
            os._exit(127)
    main_code, reaped = None, {}
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        code = os.waitstatus_to_exitcode(status)
        if pid == child:
            main_code = code
        else:
            reaped[str(pid)] = code
    with open(report, "w") as stream:
        json.dump({"main": main_code, "reaped": reaped}, stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
