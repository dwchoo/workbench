"""CW-06 UX regression: repeated colours must be cheap without changing rendering.

Assertions cover colour/palette semantics, bounded retained state and the real
cell-attribute path. The optional --benchmark runs product.view.draw in owned
180x48 curses PTYs; it is a controlled-frame measurement, not an htop or mux test.
"""
from __future__ import annotations

import curses
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

import pyte
from wcwidth import wcswidth

from independent_support_cw06 import RecordingSender, SID, snap
from workbench.contracts import ui_v1
from workbench.contracts.v1 import DisplayChunk, PaneId
from workbench.ui.product.model import ProductModel
from workbench.ui.product.view import draw, pane_rects
from workbench.ui.terminal_g1 import app as renderer


class RepeatedColourTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(renderer._color_index.cache_clear)
        renderer._color_index.cache_clear()

    def test_repeated_styled_cells_do_not_repeat_nearest_palette_search(self):
        screen = pyte.Screen(40, 2)
        pyte.Stream(screen).feed(
            "\x1b[1;3;4;5;7;9;38;2;18;52;86;48;2;48;48;48m한글 e\u0301🙂"
            "\x1b[0;38;2;200;30;140;48;2;17;17;17m CPU 12%"
        )
        cells = [cell for line in screen.buffer.values() for cell in line.values() if cell.data]
        colours = renderer._ColorPairs.__new__(renderer._ColorPairs)
        colours.enabled, colours.colors, colours.pair_limit = True, 256, 256
        colours.pairs, colours.next_pair = {}, 1
        with patch.object(renderer, "_rgb_index", wraps=renderer._rgb_index) as nearest, \
                patch.object(curses, "init_pair"), \
                patch.object(curses, "color_pair", side_effect=lambda n: n << 8):
            first = [renderer._attributes(cell, colours) for cell in cells]
            initial_searches = nearest.call_count
            self.assertEqual(initial_searches, 4)
            for _ in range(100):
                self.assertEqual([renderer._attributes(cell, colours) for cell in cells], first)
            self.assertEqual(nearest.call_count, initial_searches,
                             "the same cell styles repeated per frame must reuse palette conversion")
        self.assertTrue(first[0] & curses.A_BOLD)
        self.assertTrue(first[0] & curses.A_UNDERLINE)
        self.assertTrue(first[0] & curses.A_REVERSE)
        self.assertTrue(first[0] & curses.A_BLINK)
        # pyte composes the combining accent before the renderer receives cells.
        self.assertEqual("".join(cell.data for cell in cells), "한글 é🙂 CPU 12%")

    def test_palette_size_is_part_of_mapping_identity_in_both_orders(self):
        expected = {("303030", 256): 236, ("303030", 16): 15, ("303030", 8): 4,
                    ("196", 256): 196, ("196", 16): 15, ("196", 8): 4,
                    ("brightcyan", 256): 14, ("brightcyan", 16): 14, ("brightcyan", 8): 6,
                    ("nonsense", 256): -1, ("nonsense", 8): -1}
        for queries in (list(expected), list(reversed(expected))):
            renderer._color_index.cache_clear()
            for _ in range(3):
                for query in queries:
                    self.assertEqual(renderer._color_index(*query), expected[query], query)

    def test_many_distinct_colours_have_a_finite_retention_bound(self):
        bound = renderer._color_index.cache_info().maxsize
        self.assertIsInstance(bound, int)
        self.assertGreater(bound, 0)
        self.assertLessEqual(bound, 8192, "small pure mapping cache must not retain arbitrary colour history")
        for index in range(bound + 32):
            renderer._color_index(str(1_000_000 + index), 256)
        self.assertEqual(renderer._color_index.cache_info().currsize, bound)
        # Eviction does not change results, and a different palette is still independent.
        self.assertEqual(renderer._color_index("1000000", 8), 0)
        self.assertEqual(renderer._color_index("1000000", 256), 255)


def _feed(model, pane, data, sequence):
    encoded = ui_v1.encode_display(DisplayChunk(session_id=SID, session_generation=1,
                                               pane_id=pane, sequence=sequence, data=data), replay=False)
    model.on_display(ui_v1.FrameDecoder().feed(encoded)[0])
    while model.has_backlog():
        model.feed_pending()


def _fixture():
    model = ProductModel(RecordingSender(), 48, 180, clock=lambda: 1000.0)
    model.attach_done(snap(focus="host_shell"))
    model.after_attach()
    rgb = ((18, 52, 86), (200, 30, 140), (48, 48, 48), (255, 128, 0))
    for pane in PaneId:
        rows, cols = model.sizes[pane]
        lines = []
        for row in range(rows + 60):
            red, green, blue = rgb[row % len(rgb)]
            lead = f"한글 e\u0301🙂 {pane.value} row {row:03d} CPU 42% "
            text = lead + "█" * max(0, cols - wcswidth(lead) - 2)
            sgr = f"\x1b[0;1;38;2;{red};{green};{blue};48;2;17;17;17m"
            lines.append(sgr + text + "\r\n")
        _feed(model, pane, "".join(lines).encode(), 1)
    return model


def _window_digest(window):
    """Curses' actual retained text bytes and attributes, including wide-cell layout."""
    rows, cols = window.getmaxyx()
    payload = [(window.instr(y, 0).hex(),
                [window.inch(y, x) & curses.A_ATTRIBUTES for x in range(cols)]) for y in range(rows)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _curses_child(mode, scenario, destination):
    cached = renderer._color_index
    renderer._color_index = cached.__wrapped__ if mode == "baseline" else cached
    cached.cache_clear()
    result = {}

    def run(window):
        assert window.getmaxyx() == (48, 180)
        colours = renderer._ColorPairs()
        assert colours.enabled and colours.colors >= 256
        model = _fixture()
        if scenario == "scroll":
            model.scroll_by(20, PaneId.HOST_SHELL)
        # A held mouse selection uses the public input path and stays in the host pane.
        top, left, _, _ = pane_rects(48, 180)[PaneId.HOST_SHELL]
        model.handle_input(f"\x1b[<0;{left + 2};{top + 2}M".encode(), now=1.0)
        model.handle_input(f"\x1b[<32;{left + 25};{top + 2}M".encode(), now=1.1)
        assert model.selection_spans(PaneId.HOST_SHELL, model.sizes[PaneId.HOST_SHELL][0])
        frame_hashes, wall, cpu = [], [], []
        for frame in range(16):
            if scenario == "scroll":
                model.scroll_by(1 if frame % 2 == 0 else -1, PaneId.HOST_SHELL)
            else:
                # Coloured cursor-addressed values like a process monitor, no shell/htop process.
                for pane in PaneId:
                    _feed(model, pane,
                          f"\x1b[3;1H\x1b[38;2;200;30;140m한글 e\u0301🙂 CPU {frame:02d}% MEM {frame * 3:02d}%".encode(),
                          frame + 2)
            started, cpu_started = time.perf_counter(), time.process_time()
            draw(window, model, colours)
            wall.append(time.perf_counter() - started)
            cpu.append(time.process_time() - cpu_started)
            frame_hashes.append(_window_digest(window))
        result.update(mode=mode, scenario=scenario, frames=len(wall), rows=48, cols=180,
                      wall_seconds=wall, cpu_seconds=cpu, frame_hashes=frame_hashes,
                      wall_ms_per_frame=1000 * sum(wall) / len(wall),
                      cpu_ms_per_frame=1000 * sum(cpu) / len(cpu),
                      selection_observed=True, colours=colours.colors)

    curses.wrapper(run)
    destination.write_text(json.dumps(result, indent=2))


def run_benchmark(output):
    """Own/drain/reap each PTY. No terminal, auth, model, or user demo is reused."""
    output = output.resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {"boundary": "actual curses product.view.draw; controlled frames; no backend, htop or outer mux",
              "runs": [], "screen_equality": {}, "remaining_owned": []}
    try:
        for scenario in ("scroll", "live"):
            captures = {}
            for mode in ("baseline", "cached"):
                with tempfile.TemporaryDirectory(prefix="render-pty-", dir=output) as private:
                    root = Path(private)
                    master, slave = os.openpty()
                    process = None
                    raw = bytearray()
                    destination = root / "measure.json"
                    try:
                        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 180, 0, 0))
                        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root),
                               "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.environ["PYTHONPATH"]}
                        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                                    "--curses-child", mode, scenario, str(destination)],
                                                   stdin=slave, stdout=slave, stderr=slave, env=env,
                                                   cwd=root, start_new_session=True)
                        os.close(slave)
                        slave = -1
                        os.set_blocking(master, False)
                        deadline = time.monotonic() + 60
                        eof = False
                        while not eof:
                            if time.monotonic() > deadline:
                                raise TimeoutError("controlled curses render exceeded 60s")
                            if select.select([master], [], [], 0.05)[0]:
                                try:
                                    data = os.read(master, 65536)
                                    if not data:
                                        eof = True
                                    else:
                                        raw.extend(data)
                                except OSError as error:
                                    if error.errno == errno.EIO:
                                        eof = True
                                    else:
                                        raise
                            if len(raw) > 8 * 1024 * 1024:
                                raise AssertionError("PTY output unexpectedly exceeds 8MiB")
                        assert process.wait(timeout=5) == 0, raw[-4000:].decode(errors="replace")
                        measure = json.loads(destination.read_text())
                        measure.update(pid=process.pid, process_reaped=True,
                                       output_bytes=len(raw), output_sha256=hashlib.sha256(raw).hexdigest())
                        report["runs"].append(measure)
                        captures[mode] = bytes(raw)
                    finally:
                        if process is not None and process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                        if slave >= 0:
                            os.close(slave)
                        os.close(master)
            pair = [run for run in report["runs"] if run["scenario"] == scenario]
            assert pair[0]["frame_hashes"] == pair[1]["frame_hashes"], "actual window content/attributes differ"
            assert captures["baseline"] == captures["cached"], "emitted curses terminal bytes differ"
            assert "한글 é🙂".encode() in captures["cached"], "Unicode fixture did not reach terminal"
            report["screen_equality"][scenario] = {"all_frame_text_and_attributes_equal": True,
                                                     "terminal_output_byte_equal": True}
        report["result"] = "passed"
    except BaseException as error:
        report["result"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["owned_temp_directories_removed"] = not any(output.glob("render-pty-*"))
        (output / "report.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--curses-child":
        _curses_child(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--benchmark":
        result = run_benchmark(Path(sys.argv[2]))
        print(json.dumps({"result": result["result"], "runs": [
            {key: row[key] for key in ("mode", "scenario", "wall_ms_per_frame", "cpu_ms_per_frame")}
            for row in result["runs"]]}, indent=2))
    else:
        unittest.main()
