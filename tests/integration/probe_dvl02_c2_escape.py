"""Diagnostic-only, one original tmux/dash C1-C7 cell with C2 Esc boundary trace.

The original C2 inputs, gaps, waits and assertions are untouched. This probe is
not a precise-gate result. All child hooks live in a disposable private path;
product source files are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cw16_harness as h  # noqa: E402
import live_cw16_compat as live  # noqa: E402


SITE_HOOK = r'''"""Private C2 diagnostic hook, imported only by isolated Workbench children."""
import json
import os
from pathlib import Path
import time

_root = Path(os.environ["WB_DVL_C2_HOOK"])
_count = 0
_armed_until = 0.0
_last_screen = None

def emit(event, **fields):
    global _count
    if _count >= 1800:
        return
    _count += 1
    row = {"t_ns": time.monotonic_ns(), "pid": os.getpid(), "event": event, **fields}
    path = _root / ("child-" + str(os.getpid()) + ".jsonl")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass

def pname(pane):
    return getattr(pane, "value", str(pane))

def relevant(data):
    return isinstance(data, (bytes, bytearray)) and b"\x1b" in data and len(data) <= 12

from workbench.ui.product.input import InputParser, Passthrough
from workbench.ui.product.model import ProductModel
from workbench.contracts.ui_v1 import ClientType
from workbench.backend.panes import OmpPane
import workbench.ui.product.app as app

_read = os.read
def read(fd, n):
    data = _read(fd, n)
    if fd == 0 and relevant(data):
        emit("product_stdin_read", length=len(data), hex=data.hex())
    return data
os.read = read

_feed = InputParser.feed
def feed(self, data, *, now=None):
    events = _feed(self, data, now=now)
    if relevant(data):
        emit("parser_feed", hex=data.hex(), pending=bytes(self._pending).hex(),
             prefix=self.prefix_active, events=[type(e).__name__ + ":" + e.data.hex()
             if isinstance(e, Passthrough) else type(e).__name__ for e in events])
    return events
InputParser.feed = feed

_flush = InputParser.flush
def flush(self, *, now=None):
    before = bytes(self._pending)
    events = _flush(self, now=now)
    if relevant(before) or any(isinstance(e, Passthrough) and relevant(e.data) for e in events):
        emit("parser_flush", before=before.hex(), pending=bytes(self._pending).hex(),
             prefix=self.prefix_active, events=[type(e).__name__ + ":" + e.data.hex()
             if isinstance(e, Passthrough) else type(e).__name__ for e in events])
    return events
InputParser.flush = flush

_send = ProductModel._send
def send(self, kind, payload=b"", pane=None, **fields):
    global _armed_until
    result = _send(self, kind, payload, pane, **fields)
    if kind is ClientType.INPUT and relevant(payload):
        _armed_until = time.monotonic() + 8.0
        emit("product_input_send", hex=payload.hex(), pane=pname(pane), focus=pname(self.focus),
             request_id=result, menu_open=self.menu_open, help_open=self.help_open,
             scroll_mode=pname(self._mode) if self._mode else None)
    return result
ProductModel._send = send

_result = ProductModel.on_result
def on_result(self, header):
    pending = self.pending.get(header.get("id"))
    if pending and pending[0] is ClientType.INPUT and time.monotonic() < _armed_until:
        emit("backend_result", request_id=header.get("id"), pane=pname(pending[1]),
             ok=header.get("ok"), accepted_bytes=header.get("accepted_bytes"),
             reason=header.get("reason"))
    return _result(self, header)
ProductModel.on_result = on_result

def screen_state(model):
    from workbench.contracts.v1 import PaneId
    pane = PaneId.MANAGER_OMP
    lines = model.panes[pane].screen.display
    return {"manager_hotkeys": any("hotkeys" in x for x in lines),
            "manager_hot": any("/hot" in x for x in lines),
            "backlog_bytes": model.panes[pane].backlog_bytes,
            "focus": pname(model.focus), "workbench_menu_open": model.menu_open,
            "scrolled": pane in model._views}

_enqueue = ProductModel.enqueue_display
def enqueue(self, frame):
    if time.monotonic() < _armed_until and frame.header.get("pane") == "manager_omp":
        emit("manager_display_frame", bytes=len(frame.payload), sequence=frame.header.get("sequence"),
             generation=frame.header.get("generation"), replay=frame.header.get("replay"))
    return _enqueue(self, frame)
ProductModel.enqueue_display = enqueue

_pending = ProductModel.feed_pending
def feed_pending(self, *args, **kwargs):
    global _last_screen
    result = _pending(self, *args, **kwargs)
    if time.monotonic() < _armed_until:
        state = screen_state(self)
        if state != _last_screen:
            emit("manager_pane_applied", **state)
            _last_screen = state
    return result
ProductModel.feed_pending = feed_pending

_draw = app.draw
def draw(win, model, colors):
    result = _draw(win, model, colors)
    if time.monotonic() < _armed_until:
        emit("product_draw", **screen_state(model))
    return result
app.draw = draw

_admit = OmpPane.admit
def admit(self, data):
    global _armed_until
    watch = pname(self.pane_id) == "manager_omp" and data == b"\x1b"
    if watch:
        _armed_until = time.monotonic() + 8.0
        emit("omp_admit_before", pane=pname(self.pane_id), pending=len(self._pending),
             generation=self.generation, session_id=self.session_id)
    result = _admit(self, data)
    if watch:
        emit("omp_admit_after", result=str(result) if result else None,
             pending=len(self._pending), dropped=self.dropped_input_bytes)
    return result
OmpPane.admit = admit

_pty_flush = OmpPane._flush
def pty_flush(self):
    before = bytes(self._pending)
    watch = pname(self.pane_id) == "manager_omp" and relevant(before)
    dropped_before = self.dropped_input_bytes
    result = _pty_flush(self)
    if watch:
        emit("omp_pty_flush", pending_before=len(before), pending_after=len(self._pending),
             prefix_hex=before[:12].hex(), removed=len(before) - len(self._pending),
             dropped_delta=self.dropped_input_bytes - dropped_before,
             problem=self.last_input_problem)
    return result
OmpPane._flush = pty_flush

_pump = OmpPane.pump
def pump(self):
    chunks = _pump(self)
    if pname(self.pane_id) == "manager_omp" and time.monotonic() < _armed_until and chunks:
        emit("omp_pty_output", chunks=len(chunks), bytes=sum(len(c.data) for c in chunks),
             sequences=[c.sequence for c in chunks])
    return chunks
OmpPane.pump = pump
'''


def _private(path: Path) -> None:
    info = path.lstat()
    if (not path.is_absolute() or path.resolve() != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError(f"unsafe private directory: {path}")


def _positions(lines: list[str]) -> dict[str, list[list[int]]]:
    return {token: [[row, line.find(token)] for row, line in enumerate(lines) if token in line]
            for token in ("/hot", "hotkeys")}


def assess_manager_esc_trace(events: list[dict]) -> dict:
    """Check capture completeness, not whether Esc worked or where a missing byte went."""
    starts = [e for e in events if e.get("event") == "esc_before" and e.get("pane") == "manager_omp"
              and e.get("hex") == "1b"
              and type(e.get("t_ns")) is int]
    starts.sort(key=lambda e: e["t_ns"])
    if not starts:
        return {"status": "insufficient", "missing": ["manager_esc_window"],
                "interpretation": "missing capture is unknown, not proof of input loss"}
    start = starts[0]
    ends = [e for e in events if e.get("event") == "esc_close_wait_after" and e.get("pane") == "manager_omp"
            and type(e.get("t_ns")) is int and e["t_ns"] >= start["t_ns"]]
    if not ends:
        return {"status": "insufficient", "missing": ["manager_esc_window"],
                "interpretation": "missing capture is unknown, not proof of input loss"}
    end = min(ends, key=lambda e: e["t_ns"])
    window = [e for e in events if type(e.get("t_ns")) is int
              and start["t_ns"] <= e["t_ns"] <= end["t_ns"]]

    def seen(name: str, predicate=lambda _e: True) -> bool:
        return any(e.get("event") == name and predicate(e) for e in window)

    def token_snapshot(value) -> bool:
        return isinstance(value, dict) and all(isinstance(value.get(key), list)
                                               for key in ("/hot", "hotkeys"))

    sends = [e for e in window if e.get("event") == "product_input_send" and e.get("hex") == "1b"
             and e.get("pane") == "manager_omp" and isinstance(e.get("request_id"), str)
             and e["request_id"]]
    request_id = sends[0]["request_id"] if sends else None
    output_sequences = {seq for e in window if e.get("event") == "omp_pty_output"
                        for seq in (e.get("sequences") if isinstance(e.get("sequences"), list) else [])
                        if type(seq) is int}
    frame_sequences = {e.get("sequence") for e in window if e.get("event") == "manager_display_frame"
                       and type(e.get("sequence")) is int}
    matched_sequences = sorted(output_sequences & frame_sequences)
    captures = {
        "harness_send": seen("harness_send_before", lambda e: e.get("hex") == "1b")
                        and seen("harness_send_complete", lambda e: e.get("hex") == "1b"),
        "product_stdin": seen("product_stdin_read", lambda e: e.get("hex") == "1b"),
        "parser_passthrough": seen("parser_feed", lambda e: e.get("hex") == "1b")
                              and seen("parser_flush", lambda e: isinstance(e.get("events"), list)
                                       and "Passthrough:1b" in e["events"]),
        "manager_input_request": bool(request_id),
        "backend_result_same_request": bool(request_id) and seen(
            "backend_result", lambda e: e.get("request_id") == request_id and e.get("pane") == "manager_omp"
            and type(e.get("ok")) is bool),
        "omp_admission": seen("omp_admit_before", lambda e: e.get("pane") == "manager_omp")
                         and seen("omp_admit_after", lambda e: type(e.get("pending")) is int),
        "omp_pty_write": seen("omp_pty_flush", lambda e: e.get("prefix_hex") == "1b"
                              and type(e.get("removed")) is int and e["removed"] >= 1
                              and e.get("dropped_delta") == 0),
        "matching_manager_frame": bool(matched_sequences),
        "manager_pane_applied": seen("manager_pane_applied", lambda e: type(e.get("manager_hotkeys")) is bool),
        "product_draw": seen("product_draw", lambda e: type(e.get("manager_hotkeys")) is bool),
        "esc_predicate_observed": type(end.get("predicate_result")) is bool,
        "outer_pyte_snapshot": token_snapshot(end.get("outer_positions")),
        "tmux_capture_snapshot": token_snapshot(end.get("tmux_positions")),
    }
    missing = [name for name, present in captures.items() if not present]
    return {"status": "complete" if not missing else "insufficient", "missing": missing,
            "window_start_ns": start["t_ns"], "window_end_ns": end["t_ns"],
            "request_id": request_id, "matching_frame_sequences": matched_sequences,
            "captures": captures, "interpretation": "missing capture is unknown, not proof of input loss"}


def main() -> int:
    os.umask(0o077)
    if not live.LIVE or live.SKIP or live.OUTERS != ["tmux"] or live.SHELLS != ["dash"]:
        raise ValueError("requires live, tmux-only, dash-only installed OMP cell")
    if live.DETACH_SECONDS != 65:
        raise ValueError("original C5 detach duration required")
    root = Path(os.environ["WB_DVL_NAMESPACE"])
    _private(root)
    report_dir = Path(os.environ["WB_DVL_C2_OUTPUT"])
    _private(report_dir)
    if (root / "trace").exists() or (report_dir / "result.json").exists():
        raise FileExistsError("diagnostic outputs already exist")
    if len(str(root / "wbc16tmda-XXXXXXXX" / "x.sock").encode()) >= 100:
        raise ValueError("AF_UNIX socket path would be too long")
    hook_dir = root / "hook"
    trace_dir = root / "trace"
    hook_dir.mkdir(mode=0o700)
    trace_dir.mkdir(mode=0o700)
    (hook_dir / "sitecustomize.py").write_text(SITE_HOOK)
    (hook_dir / "sitecustomize.py").chmod(0o600)

    started = time.monotonic()
    events: list[dict] = []
    state: dict[str, object] = {"pane": None, "run": None, "slash_ui": None, "esc_wait": False,
                                "watch_until": 0.0, "last_positions": None, "timed_out": False}

    def record(ui: h.Ui, event: str, **extra: object) -> None:
        positions = _positions(ui.lines())
        run = state["run"]
        tmux_positions = None
        if isinstance(run, live.CompatRun) and isinstance(run.outer, live.TmuxOuter):
            captured = run.outer.tm("capture-pane", "-p", "-t", run.outer.session, timeout=3)
            tmux_positions = _positions(captured.stdout.splitlines()) if captured.returncode == 0 else None
        events.append({"t_ns": time.monotonic_ns(), "event": event, "pane": state["pane"],
                       "ui_label": ui.label, "rendered_bytes": ui.total_bytes,
                       "outer_positions": positions, "tmux_positions": tmux_positions, **extra})

    orig_sb_init = h.Sandbox.__init__
    orig_attach_command = live.Outer.attach_command
    orig_focus = live.CompatRun.focus
    orig_type = h.Ui.type
    orig_keys = h.Ui.keys
    orig_wait = h.Ui.wait
    orig_feed = h.Ui._feed
    orig_send = h.Ui.send
    orig_run = h.StepRunner.run

    def sb_init(sb: h.Sandbox, *args, **kwargs):
        kwargs.setdefault("root_parent", root)
        orig_sb_init(sb, *args, **kwargs)
        sb.env["PYTHONPATH"] = str(hook_dir) + os.pathsep + sb.env["PYTHONPATH"]
        sb.env["WB_DVL_C2_HOOK"] = str(trace_dir)

    def attach_command(outer: live.Outer) -> str:
        command = orig_attach_command(outer)
        return command.replace("env ", f"env WB_DVL_C2_HOOK={shlex.quote(str(trace_dir))} ", 1)

    def focus(run: live.CompatRun, pane: str) -> dict:
        result = orig_focus(run, pane)
        state["run"], state["pane"] = run, pane
        return result

    def typed(ui: h.Ui, value: str | bytes, *, gap: float = 0.02) -> None:
        orig_type(ui, value, gap=gap)
        if value == "/hot" and state["pane"] in ("manager_omp", "worker_omp"):
            state["slash_ui"] = ui
            record(ui, "slash_typed", gap=gap)

    def keys(ui: h.Ui, *parts: bytes, gap: float = 0.2) -> None:
        target = state["slash_ui"] is ui and state["pane"] in ("manager_omp", "worker_omp")
        if target and parts == (b"\x1b",):
            record(ui, "esc_before", hex="1b", gap=gap)
            state["watch_until"] = time.monotonic() + 5
        orig_keys(ui, *parts, gap=gap)
        if target and parts == (b"\x1b",):
            record(ui, "esc_after_settle", hex="1b", gap=gap)
            state["esc_wait"] = True

    def sent(ui: h.Ui, data: bytes, *, chunk: int = 4096, settle: float = 0.15) -> None:
        watch = state["slash_ui"] is ui and data == b"\x1b"
        if watch:
            record(ui, "harness_send_before", hex="1b", fd=ui.fd, settle=settle)
        orig_send(ui, data, chunk=chunk, settle=settle)
        if watch:
            record(ui, "harness_send_complete", hex="1b", fd=ui.fd, settle=settle)

    def fed(ui: h.Ui, data: bytes) -> None:
        orig_feed(ui, data)
        if state["slash_ui"] is ui and time.monotonic() < state["watch_until"]:
            positions = _positions(ui.lines())
            if positions != state["last_positions"]:
                state["last_positions"] = positions
                events.append({"t_ns": time.monotonic_ns(), "event": "outer_frame_changed",
                               "pane": state["pane"], "rendered_bytes": ui.total_bytes,
                               "frame_bytes": len(data), "outer_positions": positions})

    def wait(ui: h.Ui, predicate, timeout: float = 30) -> bool:
        esc_wait = state["slash_ui"] is ui and state["esc_wait"]
        if esc_wait:
            record(ui, "esc_close_wait_before", timeout=timeout)
        result = orig_wait(ui, predicate, timeout)
        if esc_wait:
            record(ui, "esc_close_wait_after", timeout=timeout, predicate_result=result)
            state["esc_wait"] = False
            state["slash_ui"] = None
        return result

    def run(runner: h.StepRunner, name: str, fn, *, requires=()):
        if state["timed_out"]:
            runner.status[name] = h.NOT_RUN
            runner.report.step(name, h.NOT_RUN, reason="diagnostic 300s deadline")
            return h.NOT_RUN
        return orig_run(runner, name, fn, requires=requires)

    def deadline(_signum, _frame):
        state["timed_out"] = True
        raise TimeoutError("diagnostic 300s cell deadline")

    h.Sandbox.__init__ = sb_init
    live.Outer.attach_command = attach_command
    live.CompatRun.focus = focus
    h.Ui.type = typed
    h.Ui.keys = keys
    h.Ui.wait = wait
    h.Ui._feed = fed
    h.Ui.send = sent
    h.StepRunner.run = run
    old_handler = signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 300)
    code = 1
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(
            "live_cw16_compat.Cw16CompatMatrix.test_tmux_dash")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        code = 0 if result.wasSuccessful() and result.testsRun == 1 else 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        h.Sandbox.__init__ = orig_sb_init
        live.Outer.attach_command = orig_attach_command
        live.CompatRun.focus = orig_focus
        h.Ui.type = orig_type
        h.Ui.keys = orig_keys
        h.Ui.wait = orig_wait
        h.Ui._feed = orig_feed
        h.Ui.send = orig_send
        h.StepRunner.run = orig_run
        child_files = sorted(trace_dir.glob("child-*.jsonl"))
        child_events = []
        for path in child_files:
            for line in path.read_text().splitlines():
                try:
                    child_events.append(json.loads(line))
                except json.JSONDecodeError:
                    child_events.append({"event": "truncated_child_record", "file": path.name})
        all_events = sorted(events + child_events, key=lambda item: item.get("t_ns", 0))
        diagnostic = assess_manager_esc_trace(all_events)
        diagnostic_code = 0 if diagnostic["status"] == "complete" else 2
        report = h.REPORT_DIR / live.RUN_ID / "compat-tmux-dash.json"
        scenario = json.loads(report.read_text()) if report.exists() else None
        payload = {"kind": "diagnostic_only_not_gate", "candidate": os.environ.get("WB_DVL_C2_CANDIDATE"),
                   "test": "Cw16CompatMatrix.test_tmux_dash", "run_id": live.RUN_ID,
                   "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": state["timed_out"],
                   "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (
                       Path(__file__), Path(live.__file__), Path(h.__file__),
                       h.SRC / "workbench/ui/product/app.py", h.SRC / "workbench/ui/product/model.py",
                       h.SRC / "workbench/ui/product/input.py", h.SRC / "workbench/backend/panes.py")},
                   "omp": h.find_omp(), "omp_version": scenario.get("versions", {}).get("omp") if scenario else None,
                   "scenario": str(report), "steps": scenario.get("steps") if scenario else None,
                   "cleanup": scenario.get("cleanup") if scenario else None,
                   "original_cell_exit_code": code, "diagnostic": diagnostic,
                   "diagnostic_exit_code": diagnostic_code, "process_exit_code": code or diagnostic_code,
                   "harness_events": len(events), "child_events": len(child_events), "events": all_events}
        target = report_dir / "result.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return code or diagnostic_code


if __name__ == "__main__":
    raise SystemExit(main())
