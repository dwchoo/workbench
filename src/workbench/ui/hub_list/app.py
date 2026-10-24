"""Hub list app: polls the Hub, runs the model's actions outside curses.

``run_hub_list`` returns an exit code: 0 on ``q``, 1 when the Hub is not available or the app is
terminated, 2 without a TTY. The list lives in curses; every action (``Attach`` → ``run_product``,
``PowerOn`` → ``projects.power_on``, ``PowerOff`` → ``projects.power_off``) runs with curses ended
(plain terminal) and the list is entered again afterwards with the result as its notice.
"""
from __future__ import annotations

import curses
import io
import json
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
from typing import Callable

from workbench.hub import projects, registry
from workbench.hub.client import HubClient
from workbench.hub.paths import hub_layout
from workbench.hub.process import HubNotRunning, HubUnknown, HubUnresponsive
from workbench.hub.status import POLL_INTERVAL, check_entry
from workbench.ui.hub_list.model import Attach, HubListModel, PowerOff, PowerOn, Quit, Refresh
from workbench.ui.hub_list.view import draw

LIST_TIMEOUT = 30.0  # same bound as ``wb hub list``
NO_TTY_TEXT = "Hub 목록은 terminal이 필요합니다. 'wb hub list'를 쓰세요"
BACK_PROMPT = "Enter를 누르면 목록으로 돌아갑니다. "
REGISTRY_BAD_TEXT = "Hub registry가 손상되어 목록을 읽지 못했습니다"


class Terminated(Exception):
    """SIGTERM/SIGHUP while the list is up: curses is ended on the way out."""


class HubPoller(threading.Thread):
    """Asks the Hub for ``list`` every :data:`POLL_INTERVAL` and on :meth:`refresh`; results are
    ``("rows", rows, notice)`` or ``("lost", reason)`` events for :meth:`drain`. A slow Hub never
    blocks the screen: the request runs here, the screen only reads events."""

    def __init__(self, fetch: Callable[[bool], tuple] | None = None, interval: float = POLL_INTERVAL):
        super().__init__(daemon=True, name="hub-list-poller")
        self.fetch = fetch or fetch_list
        self.interval = interval
        self.events: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._idle = threading.Event()  # set while polling is paused for an action
        self._fresh = False

    def refresh(self, fresh: bool = True) -> None:
        self._fresh = self._fresh or fresh
        self._wake.set()

    def pause(self) -> None:
        self._idle.set()

    def resume(self) -> None:
        self._idle.clear()
        self.refresh()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def drain(self) -> list[tuple]:
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    def run(self) -> None:
        while not self._stopping.is_set():
            if not self._idle.is_set():
                fresh, self._fresh = self._fresh, False
                self.events.put(self.fetch(fresh))
            self._wake.wait(self.interval)
            self._wake.clear()


def fetch_list(fresh: bool) -> tuple:
    """One Hub ``list``: ``("rows", rows, notice)`` or ``("lost", reason)``."""
    try:
        with HubClient(hub_layout(), timeout=LIST_TIMEOUT) as client:
            reply = client.request("list", fresh=True) if fresh else client.request("list")
    except HubNotRunning:
        return ("lost", "not_running")
    except (HubUnknown, HubUnresponsive) as exc:
        return ("lost", exc.reason)
    except OSError as exc:
        return ("lost", type(exc).__name__)
    if not reply.get("ok"):
        return ("lost", str(reply.get("reason")))
    notice = ""
    if (reply.get("registry") or {}).get("state") != "ok":
        notice = f"{REGISTRY_BAD_TEXT}: {(reply.get('registry') or {}).get('path')}"
    return ("rows", reply.get("rows") or [], notice)


def key_name(key: object) -> str:
    """The model's key name for what ``get_wch`` returned."""
    if key == curses.KEY_UP:
        return "up"
    if key == curses.KEY_DOWN:
        return "down"
    if key in (curses.KEY_ENTER, "\n", "\r"):
        return "enter"
    if key == "\x1b":
        return "esc"
    return key if isinstance(key, str) else ""


def _screen(stdscr: "curses.window", model: HubListModel, poller: HubPoller):
    """The list until the model asks for an action; ``Refresh`` is served here and the loop goes on."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    curses.set_escdelay(25)
    stdscr.keypad(True)
    stdscr.timeout(100)
    while True:
        for event in poller.drain():
            if event[0] == "rows":
                model.set_rows(event[1])
                if event[2]:
                    model.notice = event[2]
            else:
                model.set_disconnected()
        draw(stdscr, model, time.time())
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue  # the 100 ms timeout: poll the events again
        except KeyboardInterrupt:
            return Quit()
        action = model.press(key_name(key))
        if isinstance(action, Refresh):
            poller.refresh(fresh=True)
            continue
        if action is not None:
            return action


def _wait_enter(prompt: str = BACK_PROMPT) -> None:
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()


def _ask_stop(active: list) -> bool:
    print("active work:" if active else "no active work reported")
    for item in active:
        print(f"  - {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
    try:
        return input("이 프로젝트의 backend, 두 OMP session과 host shell을 끌까요? [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else ""


def _attach(entry: registry.Entry, model: HubListModel) -> int | None:
    """``run_product`` for the backend verified right now; None keeps the list, 2 ends the app."""
    from workbench.backend.paths import DataDirError, DataLayout
    from workbench.backend.process_identity import BackendIdentityError, inspect_backend
    from workbench.ui.product import run_product
    name = f'"{entry.name}"'
    mismatch = check_entry(entry)
    if mismatch is not None:
        model.notice = f"{name}: 불일치({mismatch})라 들어갈 수 없습니다"
        return None
    try:
        identity = inspect_backend(DataLayout(Path(entry.root) / ".wb"))
    except (BackendIdentityError, DataDirError, OSError, ValueError) as exc:
        model.notice = f"{name}: backend를 확인하지 못했습니다({type(exc).__name__})"
        return None
    if identity.state != "running":
        model.notice = f"{name}은(는) 실행 중이 아니라 들어갈 수 없습니다"
        return None
    status = run_product(identity.layout.ui_socket, expected_process=identity.record["processes"]["backend"])
    if status == 2:
        return 2
    model.notice = (f"{name}에서 detach했습니다. 프로젝트는 계속 실행됩니다" if status == 0 else
                    f"{name} 연결이 끝났습니다(종료되었거나 오류). 목록을 새로 고칩니다")
    return None


def _power_on(action: PowerOn, model: HubListModel) -> int | None:
    entry = action.entry
    name = f'"{entry.name}"'
    print(f"{name} 켜는 중…", flush=True)
    buffer = io.StringIO()
    try:
        outcome = projects.power_on(entry, False, out=buffer)
    except KeyboardInterrupt:
        model.notice = f"{name} 켜기를 중단했습니다(이미 시작된 backend는 계속 실행될 수 있습니다)"
        return None
    output = buffer.getvalue()
    if not outcome.ok:
        if output.strip():
            print(output.rstrip())
        print(f"{name}을(를) 켜지 못했습니다: {outcome.reason}: {outcome.detail}")
        _wait_enter()
        model.notice = f"{name}을(를) 켜지 못했습니다({outcome.reason})"
        return None
    if action.attach:
        return _attach(entry, model)
    model.notice = f"{name}을(를) 켰습니다" + (f" · {_first_line(output)}" if _first_line(output) else "")
    return None


def _power_off(action: PowerOff, model: HubListModel) -> None:
    entry = action.entry
    name = f'"{entry.name}"'
    try:
        outcome = projects.power_off(entry, _ask_stop, out=sys.stdout)
    except KeyboardInterrupt:
        print()
        model.notice = f"{name} 끄기를 중단했습니다"
        return
    if outcome.ok:
        model.notice = f"{name}을(를) 껐습니다"
    elif outcome.reason == projects.DECLINED:
        model.notice = f"{name}: 취소했습니다(아무것도 끄지 않았습니다)"
    else:
        print(f"{name}을(를) 끄지 못했습니다: {outcome.reason}: {outcome.detail}")
        _wait_enter()
        model.notice = f"{name}을(를) 끄지 못했습니다({outcome.reason})"


def execute(action: object, model: HubListModel) -> int | None:
    """Run one action with curses ended; an exit code ends the app, None returns to the list."""
    if isinstance(action, Attach):
        return _attach(action.entry, model)
    if isinstance(action, PowerOn):
        return _power_on(action, model)
    if isinstance(action, PowerOff):
        _power_off(action, model)
    return None


def _hub_unavailable() -> int | None:
    """None when a verified Hub answers; otherwise the exit code after saying why (as ``wb hub list``)."""
    from workbench.hub.cli import EXIT_FAILURE, EXIT_REQUIREMENT, MANAGED_TREE_TEXT, NOT_RUNNING_TEXT
    layout = hub_layout()
    try:
        HubClient(layout, timeout=5.0).close()
    except HubNotRunning:
        from workbench.hub.process import managed_ancestor
        if managed_ancestor(os.getpid()) is not None:
            print(MANAGED_TREE_TEXT, file=sys.stderr)
            return EXIT_REQUIREMENT
        print(f"{NOT_RUNNING_TEXT}: Hub를 자동 시작하지 못해 목록을 볼 수 없습니다. 'wb hub start'를 실행하세요.",
              file=sys.stderr)
        return EXIT_FAILURE
    except (HubUnknown, HubUnresponsive) as exc:
        print(f"Hub에 묻지 못했습니다: {exc.reason} (lock {layout.lock})", file=sys.stderr)
        return EXIT_FAILURE
    return None


def run_hub_list(*, stdin_fd: int = 0, stdout_fd: int = 1, poller: HubPoller | None = None) -> int:
    if not os.isatty(stdin_fd) or not os.isatty(stdout_fd):
        print(NO_TTY_TEXT, file=sys.stderr)
        return 2
    if poller is None:
        failed = _hub_unavailable()
        if failed is not None:
            return failed
        poller = HubPoller()

    def on_signal(signum: int, _frame: object) -> None:
        raise Terminated(signum)

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGHUP)}
    model = HubListModel()
    poller.start()
    try:
        while True:
            action = curses.wrapper(_screen, model, poller)
            if isinstance(action, Quit):
                return 0
            poller.pause()
            try:
                code = execute(action, model)
            finally:
                poller.resume()
            if code is not None:
                return code
    except Terminated:
        return 1
    finally:
        poller.stop()
        poller.join(timeout=2.0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
