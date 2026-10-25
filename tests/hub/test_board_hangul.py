"""Korean (Hangul) text through the Board terminal (seam T5: ``GET /ws/board`` over a real :class:`BoardSet`).

The channels run :mod:`test_board_ws`'s recorder PTY command (raw mode; it writes every byte it reads to a file and
echoes ``<bytes>``). Expected values are the UTF-8 encoding of the test's own text, cut by the test on code point
boundaries into frames of at most 64 KiB (SPEC "입력 한도"), and the bytes the recorder wrote.
Run with ``discover -s tests/hub -p 'test_board_hangul.py'``.
"""
from __future__ import annotations

import asyncio
import shutil
import textwrap
import time
import unittest

import test_board_ws as board_case  # module import: its test classes stay uncollected here
import test_board_launch as launch_case
import test_board_set as set_case

from workbench.hub.board.set import Ring

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
        typed = '안녕하세요 ㄱㅏ \u1112\u1161\u11ab \u3164 👍🏽'.encode('utf-8')
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


STALLING_RECORDER = textwrap.dedent('''\
    import os, sys, time, tty
    record, stall = sys.argv[1], float(os.environ.get('STALL', '0'))
    try:
        tty.setraw(0)
    except Exception:
        pass
    os.write(1, b"ready:" + sys.argv[2].encode() + b"\\r\\n")
    out = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    first = True
    while True:
        data = os.read(0, 65536)
        if not data:
            break
        os.write(out, data)
        if first:  # a program busy with something else: its terminal input piles up meanwhile
            first = False
            time.sleep(stall)
    ''')


class PasteBackpressureTests(board_case.BoardWsCase):
    """A paste bigger than the PTY takes at once is paced (the Hub stops reading the socket), never dropped."""

    stall = 0.0

    async def attached(self):
        self.launcher.script.write_text(STALLING_RECORDER)  # the set starts at the first hello
        original = self.launcher.started

        def started(name):
            launch = original(name)
            launch.env['STALL'] = str(self.stall)
            return launch
        self.launcher.started = started
        tab = self.tab()
        epoch = (await tab.attach(self.session.csrf))['epoch']
        await tab.wait_output('shell', b'ready:shell')
        return tab, epoch

    async def send_pastes(self, tab, epoch, pastes):
        for paste in pastes:
            for frame in code_point_frames(paste):
                await tab.send_input('shell', epoch, frame)

    async def test_four_three_megabyte_pastes_arrive_byte_exact_and_in_order(self):
        tab, epoch = await self.attached()
        unit = '가나다라 abc 😀\r'
        pastes = [(f'[붙여넣기 {n}] ' + unit * (3 * (1 << 20) // len(unit.encode('utf-8')))) for n in range(4)]
        await self.send_pastes(tab, epoch, pastes)
        expected = ''.join(pastes).encode('utf-8')
        self.assertGreater(len(expected), 12 * (1 << 20))
        self.assertTrue(await self.eventually(lambda: len(self.recorded('shell')) >= len(expected), 60),
                        (len(self.recorded('shell')), len(expected)))
        self.assertEqual(self.recorded('shell'), expected)
        self.assertEqual(self.terminal.dropped_inputs, 0)
        self.assertNotIn('error', [event.get('t') for event in tab.events])
        lease = self.board.status()['lease']
        self.assertEqual((lease['state'], lease['epoch']), ('connected', epoch))


class StalledPasteTests(PasteBackpressureTests):
    """The program reads nothing for longer than the heartbeat limit (2 x 2 s + 1 s) while a paste waits."""

    stall = 6.0

    async def test_a_paste_waiting_on_a_busy_program_keeps_the_lease_and_arrives_whole(self):
        tab, epoch = await self.attached()
        paste = '한글 붙여넣기 ' * (3 * (1 << 20) // 20)
        await self.send_pastes(tab, epoch, [paste])
        expected = paste.encode('utf-8')
        self.assertTrue(await self.eventually(lambda: len(self.recorded('shell')) >= len(expected), 60))
        self.assertEqual(self.recorded('shell'), expected)
        self.assertIsNone(tab.close_code)
        self.assertEqual((self.board.status()['lease']['state'], self.terminal.dropped_inputs), ('connected', 0))
        self.assertNotIn('terminal_timeout', [event for event, _ in self.audit()])

    async def test_a_new_tab_takes_over_at_once_while_the_holders_paste_is_paced(self):
        tab, epoch = await self.attached()
        sending = asyncio.ensure_future(self.send_pastes(tab, epoch, ['가' * (2 << 20)]))
        self.addAsyncCleanup(lambda: asyncio.gather(sending, return_exceptions=True))
        await asyncio.sleep(1.0)  # the program sleeps on its first read: the paste piles up in the Hub
        other = self.services.sessions.create('127.0.0.2', 'other', time.time(), generation=self.generation)
        second = self.tab(other)
        started = time.monotonic()
        attached = await second.attach(other.csrf)
        self.assertEqual(attached['epoch'], epoch + 1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(await tab.wait_closed(5), 4001)


class ReplayBoundaryTests(unittest.TestCase):
    """The attach replay is the ring's newest bytes: trimming must never start it inside a UTF-8 sequence."""

    def test_a_trimmed_ring_starts_on_a_code_point_boundary_for_every_cut_offset(self):
        for wide in ('한', 'ㄱ', '\u1161', '😀', 'é'):
            encoded = wide.encode('utf-8')
            for cut in range(len(encoded)):
                with self.subTest(wide=wide, cut=cut):
                    ring = Ring(limit=12)
                    written = b'abc' + wide.encode('utf-8') * 8
                    for start in range(0, len(written), 5):  # PTY reads in arbitrary chunks
                        ring.append(written[start:start + 5])
                    ring.append(b'x' * (12 - (len(encoded) - cut)) if cut else b'')
                    replay = ring.snapshot()
                    self.assertLessEqual(len(replay), 12)
                    everything = written + (b'x' * (12 - (len(encoded) - cut)) if cut else b'')
                    self.assertTrue(everything.endswith(replay))
                    replay.decode('utf-8')  # strict: a cut sequence raises
                    self.assertGreaterEqual(len(replay), 12 - 3)
                    self.assertEqual(ring.total, len(everything))

    def test_ascii_output_still_keeps_exactly_the_limit_and_non_utf8_bytes_are_not_eaten(self):
        ring = Ring(limit=8)
        ring.append(b'0123456789')
        self.assertEqual(ring.snapshot(), b'23456789')
        binary = Ring(limit=8)
        binary.append(b'\x00' + b'\x80' * 12)  # no lead byte anywhere: not UTF-8, keep the newest bytes
        self.assertEqual(len(binary.snapshot()), 8)


LOCALE_KEYS = ('LANG', 'LC_ALL', 'LC_CTYPE')


class BoardLocaleTests(launch_case.LauncherCase):
    """Both channels get a UTF-8 character type (the browser terminal is UTF-8), keeping the user's own."""

    def environments(self, **locale) -> list[dict]:
        for key in LOCALE_KEYS:
            self.box.env.pop(key, None)
        self.box.env.update(locale)
        launcher = self.make_launcher(self.config)
        return [launcher.prepare_shell().env, self.prepare(launcher).env]

    def test_no_locale_at_all_gets_lang_c_utf8(self):
        for env in self.environments():
            self.assertEqual(env.get('LANG'), 'C.UTF-8')
            self.assertNotIn('LC_ALL', env)

    def test_a_non_utf8_locale_in_effect_is_replaced_where_it_is_set(self):
        cases = [({'LANG': 'C'}, {'LANG': 'C.UTF-8'}),
                 ({'LANG': 'POSIX', 'LC_ALL': ''}, {'LANG': 'C.UTF-8', 'LC_ALL': ''}),
                 ({'LANG': 'ko_KR.UTF-8', 'LC_ALL': 'C'}, {'LANG': 'ko_KR.UTF-8', 'LC_ALL': 'C.UTF-8'}),
                 ({'LANG': 'ko_KR.UTF-8', 'LC_CTYPE': 'C'}, {'LANG': 'ko_KR.UTF-8', 'LC_CTYPE': 'C.UTF-8'})]
        for given, expected in cases:
            with self.subTest(given=given):
                for env in self.environments(**given):
                    self.assertEqual({key: env[key] for key in LOCALE_KEYS if key in env}, expected)

    def test_the_users_utf8_locale_is_kept(self):
        for given in ({'LANG': 'ko_KR.UTF-8'}, {'LANG': 'en_US.utf8'}, {'LANG': 'C', 'LC_ALL': 'ko_KR.UTF-8'},
                      {'LC_CTYPE': 'C.UTF-8'}):
            with self.subTest(given=given):
                for env in self.environments(**given):
                    self.assertEqual({key: env[key] for key in LOCALE_KEYS if key in env}, given)


class BoardShellHangulTests(set_case.SetCase):
    """A real shell channel started from a Hub environment without any locale (a minimal or systemd env)."""

    def setUp(self):
        super().setUp()
        for key in LOCALE_KEYS:
            self.box.env.pop(key, None)

    def run_shell(self, shell: str, typed: bytes, needle: bytes) -> bytes:
        self.box.env['SHELL'] = shell
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        self.start()
        self.assertTrue(self.board.write('shell', typed))
        self.assertTrue(self.wait(lambda: needle in self.board.replay('shell')), self.board.replay('shell')[-300:])
        return self.board.replay('shell')

    def test_the_shell_reports_a_utf8_charmap(self):
        replay = self.run_shell('/bin/sh', b'echo "map=$(locale charmap)."\r', b'map=')
        self.assertIn(b'map=UTF-8.', replay)

    @unittest.skipUnless(shutil.which('bash'), 'bash is not installed')
    def test_readline_backspace_over_a_hangul_syllable_deletes_the_whole_character(self):
        typed = 'printf "[%s]\\n" "X한글'.encode() + b'\x7f' + b'Y"\r'
        replay = self.run_shell(shutil.which('bash'), typed, b']')
        self.assertIn('[X한Y]'.encode(), replay, replay[-300:])

    def test_a_cooked_mode_line_erases_a_whole_hangul_syllable(self):
        # /bin/sh ``read`` has no line editor: the PTY line discipline does the erase (it needs IUTF8).
        typed = 'read v\rX한글'.encode() + b'\x7f' + b'Y\rprintf "<%s>\\n" "$v"\r'
        replay = self.run_shell('/bin/sh', typed, b'>\r')
        self.assertIn('<X한Y>'.encode(), replay, replay[-300:])


if __name__ == '__main__':
    unittest.main()
