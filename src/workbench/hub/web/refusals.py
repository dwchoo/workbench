"""Rate-limited hub.log lines for refused web requests (2026-10-27 사용자 요구).

A user got ``421 misdirected_request`` and hub.log said nothing. Refusals the owner may need to diagnose (a Host
that is not the listener's, a client outside ``allow_from``, a stale login or CSRF form) now write one stderr line
(stderr is hub.log in a spawned Hub). A flood can neither grow memory nor the log: at most one line per distinct
``(reason, value)`` per :attr:`RefusalLog.WINDOW` seconds and at most :attr:`RefusalLog.TOTAL_PER_WINDOW` lines per
window in total. What is not written is only counted, and the next written line says how many.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import sys
import time
from typing import Callable

VALUE_LIMIT = 100


def printable(value: object) -> str:
    """``value`` as printable ASCII only (anything else becomes ``?``), cut to :data:`VALUE_LIMIT` characters."""
    return "".join(char if " " <= char <= "~" else "?" for char in str(value)[:VALUE_LIMIT])


def sanitize(value: object) -> str:
    """:func:`printable` ``value`` quoted; ``(none)`` for ``None``."""
    return "(none)" if value is None else f"'{printable(value)}'"


def _stderr(line: str) -> None:
    try:
        print(line, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


class RefusalLog:
    WINDOW = 60.0
    TOTAL_PER_WINDOW = 20

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, write: Callable[[str], None] = _stderr):
        self.clock, self.write = clock, write
        # (reason, value) -> [time written, repeats not written]; only written lines add keys, so at most
        # TOTAL_PER_WINDOW keys live at once.
        self._seen: OrderedDict[tuple[str, str], list] = OrderedDict()
        self._written: deque[float] = deque()
        self._dropped = 0  # refusals not written and no longer attached to a live key

    def tracked(self) -> int:
        return len(self._seen)

    def _prune(self, now: float) -> None:
        while self._written and now - self._written[0] >= self.WINDOW:
            self._written.popleft()
        while self._seen:
            key, (at, repeats) = next(iter(self._seen.items()))
            if now - at < self.WINDOW:
                break
            del self._seen[key]
            self._dropped += repeats

    def note(self, reason: str, value: str, text: str) -> bool:
        """Write ``text`` unless the same ``(reason, value)`` was written within the window or the window's total
        is used up; True when written."""
        now = float(self.clock())
        self._prune(now)
        key = (reason, value)
        entry = self._seen.get(key)
        if entry is not None:
            entry[1] += 1
            return False
        if len(self._written) >= self.TOTAL_PER_WINDOW:
            self._dropped += 1
            return False
        if self._dropped:
            text += f" (+{self._dropped} more refusals not logged: rate limit)"
            self._dropped = 0
        self._seen[key] = [now, 0]
        self._written.append(now)
        self.write(text)
        return True
