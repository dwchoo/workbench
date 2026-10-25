"""Korean (Hangul) text through the Board terminal (seam T5: ``GET /ws/board`` over a real :class:`BoardSet`).

The channels run :mod:`test_board_ws`'s recorder PTY command (raw mode; it writes every byte it reads to a file and
echoes ``<bytes>``). Expected values are the UTF-8 encoding of the test's own text, cut by the test on code point
boundaries into frames of at most 64 KiB (SPEC "입력 한도"), and the bytes the recorder wrote.
Run with ``discover -s tests/hub -p 'test_board_hangul.py'``.
"""
from __future__ import annotations

import unittest

import test_board_ws as board_case  # module import: its test classes stay uncollected here

FRAME_LIMIT = 64 * 1024


def code_point_frames(text: str, limit: int = FRAME_LIMIT) -> list[bytes]:
    """``text`` as UTF-8 pieces of at most ``limit`` bytes, each a whole number of code points."""
    pieces, current, size = [], [], 0
    for char in text:
        encoded = char.encode('utf-8')
        if size + len(encoded) > limit:
            pieces.append(b''.join(current))
            current, size = [], 0
        current.append(encoded)
        size += len(encoded)
    pieces.append(b''.join(current))
    return pieces


class HangulInputTests(board_case.BoardWsCase):
    async def attached(self):
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        await tab.wait_output('agent', b'ready:agent')
        return tab, epoch

    async def test_typed_hangul_jamo_and_emoji_reach_the_pty_byte_exact_and_echo_back(self):
        tab, epoch = await self.attached()
        typed = '안녕하세요 ㄱㅏ 한 ㅤ 👍🏽'.encode('utf-8')
        await tab.send_input('agent', epoch, typed)
        await tab.wait_output('agent', b'<' + typed + b'>')
        self.assertEqual(self.recorded('agent'), typed)

    async def test_a_one_megabyte_hangul_paste_in_64_kib_frames_arrives_in_order_byte_exact(self):
        tab, epoch = await self.attached()
        unit = '가나다라마바사 abc 😀 \r'
        text = unit * ((1 << 20) // len(unit.encode('utf-8')) + 1)
        frames = code_point_frames(text)
        self.assertGreaterEqual(len(frames), 16)
        for frame in frames:
            await tab.send_input('shell', epoch, frame)
        expected = text.encode('utf-8')
        self.assertTrue(await self.eventually(lambda: len(self.recorded('shell')) >= len(expected), 20))
        self.assertEqual(self.recorded('shell'), expected)
        self.assertEqual(self.terminal.dropped_inputs, 0)
        self.assertEqual(self.board.status()['lease']['state'], 'connected')
        self.assertEqual(self.board.status()['lease']['epoch'], epoch)


if __name__ == '__main__':
    unittest.main()
