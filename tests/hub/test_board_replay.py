"""The Board attach replay (``Ring``, seam: ``BoardSet.replay`` = ``Ring.snapshot()``): a trim of the newest-bytes
window never starts the replay inside an escape sequence, and terminal modes set before the window (alternate
screen, mouse reporting, bracketed paste, application cursor keys) are set again at its start, because the page
resets xterm before every replay.

Expected values are the test's own bytes and ECMA-48 / xterm control sequence syntax (CSI ``ESC [ params
intermediates final``, OSC ``ESC ] ... BEL|ESC \\``, DECSET ``CSI ? Pm h``/DECRST ``CSI ? Pm l``).
Run with ``discover -s tests/hub -p 'test_board_replay.py'``.
"""
from __future__ import annotations

import re
import unittest

from workbench.hub.board.set import Ring

DECSET = re.compile(rb'\x1b\[\?(\d+)h')


def fill(ring: Ring, data: bytes, step: int = 7) -> None:
    for start in range(0, len(data), step):  # PTY reads come in arbitrary pieces
        ring.append(data[start:start + step])


def split_prefix(replay: bytes) -> tuple[set[int], bytes]:
    """The DECSET sequences the replay starts with, and the rest."""
    modes = set()
    while True:
        match = DECSET.match(replay)
        if match is None:
            return modes, replay
        modes.add(int(match.group(1)))
        replay = replay[match.end():]


class EscapeBoundaryTests(unittest.TestCase):
    def test_a_cut_inside_a_csi_or_osc_sequence_moves_to_a_sequence_boundary(self):
        sequences = [b'\x1b[38;5;196m', b'\x1b[12;40H', b'\x1b]0;\xed\x95\x9c\xea\xb8\x80 title\x07',
                     b'\x1b]8;;https://example.com/a\x1b\\', b'\x1b(B']
        for sequence in sequences:
            for offset in range(1, len(sequence)):
                with self.subTest(sequence=sequence, offset=offset):
                    head, tail = b'a' * 40, b'b' * 30
                    written = head + sequence + tail
                    limit = len(written) - len(head) - offset  # the plain trim would start ``offset`` bytes in
                    ring = Ring(limit=limit)
                    fill(ring, written)
                    _, rest = split_prefix(ring.snapshot())
                    self.assertIn(rest, (sequence + tail, tail))
                    self.assertEqual(ring.total, len(written))

    def test_plain_text_and_whole_sequences_keep_the_plain_window(self):
        ring = Ring(limit=10)
        fill(ring, b'0123456789\x1b[1mABCDEFGHIJ')
        self.assertEqual(ring.snapshot(), b'ABCDEFGHIJ')


class ModeReplayTests(unittest.TestCase):
    def test_modes_set_before_the_window_are_set_again_at_its_start(self):
        ring = Ring(limit=64)
        fill(ring, b'\x1b[?1049h\x1b[?1000;1006h\x1b[?2004h\x1b[?1h\x1b[?25l' + b'x' * 200)
        later = b'\x1b[?1000l' + b'y' * 40
        fill(ring, later)
        modes, rest = split_prefix(ring.snapshot())
        self.assertEqual(modes, {1049, 1000, 1006, 2004, 1})  # 25 (cursor) is not a mode the page needs back
        self.assertTrue(rest.endswith(later))
        self.assertLessEqual(len(rest), 64)

    def test_a_mode_reset_or_a_full_reset_before_the_window_is_not_set_again(self):
        ring = Ring(limit=32)
        fill(ring, b'\x1b[?1049h\x1b[?2004h\x1b[?1002h' + b'a' * 50 + b'\x1b[?1049l\x1b[?1002l' + b'b' * 80)
        self.assertEqual(split_prefix(ring.snapshot())[0], {2004})
        reset = Ring(limit=32)
        fill(reset, b'\x1b[?1049h\x1b[?2004h' + b'a' * 50 + b'\x1bc' + b'b' * 80)
        self.assertEqual(split_prefix(reset.snapshot())[0], set())

    def test_no_prefix_while_nothing_was_trimmed(self):
        ring = Ring(limit=1024)
        fill(ring, b'\x1b[?1049h\x1b[?1000h hello')
        self.assertEqual(ring.snapshot(), b'\x1b[?1049h\x1b[?1000h hello')


if __name__ == '__main__':
    unittest.main()
