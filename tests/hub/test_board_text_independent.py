"""Independent verification: Hangul input, copy/paste and scrolling in the Board agent, and Hangul in the web pages.

Derived from the user requirements (Korean input must work without bugs in the Board agent and the web; copy and
paste must work in the Board agent; scrolling must work) and the SPEC (``docs/features/task-inbox/SPEC.md``: "입력
한도" - title 1~200 code points, body <= 64 KiB and comment <= 16 KiB of UTF-8, a WebSocket input frame <= 64 KiB;
"WebSocket terminal 프로토콜"; the PTY ring replay), not from the worker's tests. Expected values come from Python's
own UTF-8 codec, an independent byte-level model of ECMA-48 / DEC private-mode state written here, and the bytes
that real child processes record.

Seams: the real ``splitInput`` of ``board-state.js`` run by Node; ``GET /ws/board/{id}`` (protocol v2, WBX-D1: one
socket per terminal, bare frames) over a real ``BoardSet`` and real PTYs; ``Ring.snapshot()``; the real shell channel (``locale charmap``, ``stty``, bash readline); the request pages
over a real inbox store. The pure key / glue / scroll checks are in ``js/board_text_independent.test.mjs``.
Run with ``discover -s tests/hub -p 'test_board_text_independent.py'``.
"""
from __future__ import annotations

import asyncio
import bisect
import codecs
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import textwrap
import unicodedata
import unittest
from urllib.parse import quote

import aiohttp

import test_board_launch as launch_case  # module imports: their test classes stay uncollected here
import test_board_set as set_case
import test_board_ws as ws_case
import test_web_pages_requests as request_case

from workbench.hub.board.set import RING_BYTES, Ring

REPO = Path(__file__).resolve().parents[2]
BOARD_STATE_JS = REPO / 'src' / 'workbench' / 'hub' / 'web' / 'static' / 'board-state.js'
FRAME_LIMIT = 64 * 1024
QUEUE_LIMIT = 4 * 1024 * 1024
HAS_NODE = shutil.which('node') is not None

# ---------------------------------------------------------------------------------------------------------------
# Random mixed text (the same families as the Node side).

EMOJI = ['😀', '👍🏽', '👨‍👩‍👧‍👦', '🇰🇷', '❤️', '1️⃣', '🧑‍💻']


def _syllable(rand: random.Random) -> str:
    return chr(rand.randint(0xAC00, 0xD7A3))


MIXED_FAMILIES = [
    _syllable,
    lambda r: unicodedata.normalize('NFD', _syllable(r)),
    lambda r: chr(r.randint(0x3131, 0x318E)),
    lambda r: chr(r.randint(0x1100, 0x11FF)),
    lambda r: r.choice(EMOJI),
    lambda r: chr(r.randint(0x4E00, 0x9FFF)),
    lambda r: chr(r.randint(0x20000, 0x2A6DF)),
    lambda r: chr(r.randint(0x20, 0x7E)),
    lambda r: chr(r.randint(0x00, 0x1F)),
    lambda r: '\x7f',
    lambda r: r.choice('éñßΩя£'),
    lambda r: '\r',
]


def mixed_bytes(seed: int, size: int) -> bytes:
    """UTF-8 of random mixed text, exactly ``size`` bytes (ASCII fills the end)."""
    rand = random.Random(seed)
    parts, used = [], 0
    while used < size:
        token = rand.choice(MIXED_FAMILIES)(rand)
        encoded = token.encode('utf-8')
        if used + len(encoded) > size:
            parts.append(b'a')
            used += 1
            continue
        parts.append(encoded)
        used += len(encoded)
    data = b''.join(parts)
    assert len(data) == size
    data.decode('utf-8')
    return data


NODE_SPLIT = textwrap.dedent('''\
    import { readFileSync } from "node:fs";
    const board = await import(process.argv[1]);
    const bytes = new Uint8Array(readFileSync(process.argv[2]));
    const out = [];
    for (const piece of board.splitInput(bytes)) {
      const head = Buffer.alloc(4);
      head.writeUInt32BE(piece.length);
      out.push(head, Buffer.from(piece));
    }
    process.stdout.write(Buffer.concat(out));
    ''')


def node_split(case: unittest.TestCase, data: bytes) -> list[bytes]:
    """The pieces the page's real ``splitInput`` makes of ``data`` (Node runs ``board-state.js`` unchanged)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix='wb-split-') as temp:
        path = Path(temp) / 'input.bin'
        path.write_bytes(data)
        done = subprocess.run(['node', '--input-type=module', '-e', NODE_SPLIT, BOARD_STATE_JS.as_uri(), str(path)],
                              capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    case.assertEqual(done.returncode, 0, done.stderr[-500:])
    pieces, out, at = [], done.stdout, 0
    while at < len(out):
        size = int.from_bytes(out[at:at + 4], 'big')
        pieces.append(out[at + 4:at + 4 + size])
        at += 4 + size
    return pieces


# ---------------------------------------------------------------------------------------------------------------
# An independent model of terminal output: tokens (whole escape sequences / UTF-8 text runs) and DEC mode state.

PAGE_MODES = frozenset({1, 1049, 1000, 1002, 1006, 2004})  # the modes the page needs back after its reset
OTHER_MODES = (25, 7, 12)  # modes that must NOT come back in a replay prefix
DECSET_PREFIX = re.compile(rb'\x1b\[\?(\d+)h')


class Token:
    __slots__ = ('data', 'effect', 'text')

    def __init__(self, data: bytes, effect=None, text: bool = False):
        self.data, self.effect, self.text = data, effect, text


def _text_token(rand: random.Random) -> Token:
    pool = [_syllable, lambda r: unicodedata.normalize('NFD', _syllable(r)), lambda r: chr(r.randint(0x3131, 0x318E)),
            lambda r: r.choice(EMOJI), lambda r: chr(r.randint(0x4E00, 0x9FFF)), lambda r: chr(r.randint(0x20, 0x7E)),
            lambda r: r.choice('éñΩ')]
    return Token(''.join(rand.choice(pool)(rand) for _ in range(rand.randint(1, 10))).encode('utf-8'), text=True)


def random_token(rand: random.Random) -> Token:
    kind = rand.random()
    if kind < 0.45:
        return _text_token(rand)
    if kind < 0.55:
        return Token(b'\x1b[%sm' % rand.choice([b'0', b'1', b'38;5;196', b'48;2;1;22;255', b'4;31']))
    if kind < 0.62:
        return Token(b'\x1b[%d;%dH' % (rand.randint(1, 50), rand.randint(1, 200)))
    if kind < 0.66:
        return Token(rand.choice([b'\x1b[K', b'\x1b[2J', b'\x1b[3A', b'\x1b(B', b'\x1b7', b'\x1b8', b'\x1b=']))
    if kind < 0.72:
        title = _text_token(rand).data
        return Token(b'\x1b]0;' + title + rand.choice([b'\x07', b'\x1b\\']))
    if kind < 0.75:
        return Token(rand.choice([b'\x1bP1$r', b'\x1b_Gi=1;']) + _text_token(rand).data + b'\x1b\\')
    if kind < 0.78:
        return Token(b'\r\n', text=True)
    if kind < 0.80:
        return Token(b'\x1bc', effect=('ris', ()))
    modes = rand.sample([1, 1049, 1000, 1002, 1006, 2004, *OTHER_MODES], rand.choice([1, 1, 1, 2, 3]))
    action = rand.choice('hl')
    return Token(b'\x1b[?' + ';'.join(map(str, modes)).encode() + action.encode(), effect=(action, tuple(modes)))


class Model:
    """A token stream with its valid replay start offsets and the mode state at any of them."""

    def __init__(self, tokens: list[Token]):
        self.stream = b''.join(token.data for token in tokens)
        self.valid = bytearray(len(self.stream) + 1)
        self.max_token = max(len(token.data) for token in tokens)
        self.starts: list[int] = []
        self.states: list[frozenset] = []
        at, state = 0, set()
        for token in tokens:
            self.valid[at] = 1
            if token.text:
                for index, byte in enumerate(token.data):
                    if byte & 0xC0 != 0x80:
                        self.valid[at + index] = 1
            if token.data[:2] in (b'\x1b]', b'\x1bP', b'\x1b_') and token.data.endswith(b'\x1b\\'):
                # A replay may begin at the string terminator ``ESC \``: a harmless ST of its own, and the
                # unfinished string's payload is gone, never shown as text.
                self.valid[at + len(token.data) - 2] = 1
            if token.effect is not None:
                action, modes = token.effect
                if action == 'ris':
                    state.clear()
                else:
                    for mode in modes:
                        (state.add if action == 'h' else state.discard)(mode)
                self.starts.append(at)
                self.states.append(frozenset(state))
            at += len(token.data)
        self.valid[len(self.stream)] = 1

    def state_at(self, offset: int) -> frozenset:
        index = bisect.bisect_left(self.starts, offset) - 1
        return self.states[index] if index >= 0 else frozenset()


def build_model(seed: int, total: int, opening: bool = True) -> Model:
    rand = random.Random(seed)
    tokens = []
    if opening:  # modes on right at the start, long before any trim point
        tokens += [Token(b'\x1b[?1049h', ('h', (1049,))), Token(b'\x1b[?1000;1006h', ('h', (1000, 1006))),
                   Token(b'\x1b[?2004h', ('h', (2004,))), Token(b'\x1b[?1h', ('h', (1,)))]
    size = sum(len(token.data) for token in tokens)
    while size < total:
        token = random_token(rand)
        tokens.append(token)
        size += len(token.data)
    return Model(tokens)


def replay_splits(replay: bytes):
    """Every way to read ``replay`` as (leading ``ESC[?Nh`` sequences, rest): the page's data may itself begin with
    mode sets, so the split is ambiguous until the model says which one is right."""
    modes, at = [], 0
    yield set(), replay
    while True:
        match = DECSET_PREFIX.match(replay, at)
        if match is None:
            return
        modes.append(int(match.group(1)))
        at = match.end()
        yield set(modes), replay[at:]


def split_replay(replay: bytes) -> tuple[set[int], bytes]:
    """Greedy: all leading ``ESC[?Nh`` sequences are the prefix (only for bodies that start with plain text)."""
    *_, last = replay_splits(replay)
    return last


def check_replay(case: unittest.TestCase, model: Model, produced: int, replay: bytes, limit: int, label: str) -> bytes:
    """``replay`` is the ring's snapshot after ``produced`` bytes of ``model.stream`` were appended. Returns the
    body after the mode prefix; fails unless some reading of the replay is a right one."""
    stream = model.stream[:produced]
    if produced <= limit:
        case.assertEqual(replay, stream, f'{label}: nothing was trimmed, so nothing is added')
        return replay
    reasons = []
    for modes, rest in replay_splits(replay):
        if not stream.endswith(rest):
            reasons.append('body is not the newest output')
            continue
        cut = produced - len(rest)
        if not model.valid[cut]:
            reasons.append(f'starts mid-sequence at {cut}: {rest[:10]!r}')
            continue
        try:
            codecs.getincrementaldecoder('utf-8')('strict').decode(rest)  # a cut-off tail may follow
        except UnicodeDecodeError:
            reasons.append('invalid UTF-8')
            continue
        if modes != set(model.state_at(cut) & PAGE_MODES):
            reasons.append(f'modes {sorted(modes)} != true {sorted(model.state_at(cut) & PAGE_MODES)} at {cut}')
            continue
        if produced > limit:
            if not limit - model.max_token - 3 <= len(rest) <= limit + model.max_token:
                reasons.append(f'window {len(rest)} for limit {limit}')
                continue
        return rest
    case.fail(f'{label}: no reading of the replay is right: {reasons}; head {replay[:40]!r}')


# ---------------------------------------------------------------------------------------------------------------
# Ring (replay) tests.

class RingReplayTests(unittest.TestCase):
    def test_every_ring_limit_and_every_append_size_replays_from_a_sequence_boundary_with_the_true_modes(self):
        for seed in range(6):
            model = build_model(seed, 700)
            for limit in range(8, 260, 7):
                for chunk in (1, 3, 11, 400, len(model.stream)):
                    ring = Ring(limit=limit)
                    for start in range(0, len(model.stream), chunk):
                        ring.append(model.stream[start:start + chunk])
                        produced = min(start + chunk, len(model.stream))
                        check_replay(self, model, produced, ring.snapshot(), limit,
                                     f'seed {seed} limit {limit} chunk {chunk} produced {produced}')
                    self.assertEqual(ring.total, len(model.stream))

    def test_larger_streams_with_random_chunks_keep_the_same_guarantees(self):
        for seed in range(100, 108):
            model = build_model(seed, 20000)
            rand = random.Random(seed)
            for limit in (64, 333, 1024, 4096):
                ring = Ring(limit=limit)
                produced = 0
                while produced < len(model.stream):
                    size = rand.randint(1, 700)
                    ring.append(model.stream[produced:produced + size])
                    produced = min(produced + size, len(model.stream))
                    if rand.random() < 0.15:
                        check_replay(self, model, produced, ring.snapshot(), limit, f'seed {seed} limit {limit}')
                check_replay(self, model, produced, ring.snapshot(), limit, f'seed {seed} limit {limit} end')

    def test_modes_enabled_before_the_trim_come_back_and_modes_disabled_later_do_not(self):
        enabled = b'\x1b[?1049h\x1b[?1000h\x1b[?1002h\x1b[?1006h\x1b[?2004h\x1b[?1h\x1b[?25l'
        ring = Ring(limit=80)
        ring.append(enabled + '한글 출력 '.encode() * 40)
        modes, rest = split_replay(ring.snapshot())
        self.assertEqual(modes, {1049, 1000, 1002, 1006, 2004, 1})
        self.assertNotIn(25, modes)
        rest.decode('utf-8')
        # now the program turns some modes off again before the window moves on
        ring.append(b'\x1b[?1000l\x1b[?1002l\x1b[?2004l' + '다음 화면 '.encode() * 40)
        modes, rest = split_replay(ring.snapshot())
        self.assertEqual(modes, {1049, 1006, 1})
        # leaving the alternate screen and a full reset clear the rest
        ring.append(b'\x1b[?1049l\x1b[?1006l' + 'x'.encode() * 200)
        self.assertEqual(split_replay(ring.snapshot())[0], {1})
        ring.append(b'\x1bc' + b'y' * 200)
        self.assertEqual(split_replay(ring.snapshot())[0], set())

    def test_a_replay_split_into_frames_at_any_offset_decodes_as_a_stream(self):
        model = build_model(7, 3000)
        ring = Ring(limit=900)
        ring.append(model.stream)
        replay = ring.snapshot()
        expected = replay.decode('utf-8')
        for offset in range(len(replay) + 1):
            decoder = codecs.getincrementaldecoder('utf-8')('strict')
            text = decoder.decode(replay[:offset]) + decoder.decode(replay[offset:], final=True)
            self.assertEqual(text, expected, offset)
        # and a non-streaming decode of a single frame is what would corrupt: the test is able to see it
        inside = next(i for i in range(len(replay)) if replay[i] & 0xC0 == 0x80)
        with self.assertRaises(UnicodeDecodeError):
            replay[:inside].decode('utf-8')


# ---------------------------------------------------------------------------------------------------------------
# Real hub + Board shell channel PTY.

EMITTER = textwrap.dedent('''\
    import os, sys, tty
    record, name, emit = sys.argv[1:4]
    tty.setraw(0)
    out = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

    def put(data):
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]

    put(b"ready:" + name.encode() + b"\\r\\n")
    first = True
    while True:
        data = os.read(0, 65536)
        if not data:
            break
        os.write(out, data)
        if first and data.startswith(b"GO"):
            first = False
            blob = open(emit, "rb").read()
            sizes = [777, 1, 4093, 3, 65536, 2, 31]
            at = index = 0
            while at < len(blob):
                put(blob[at:at + sizes[index % len(sizes)]])
                at += sizes[index % len(sizes)]
                index += 1
            put(b"done:" + name.encode() + b"\\r\\n")
        else:
            put(b"<" + data + b">")
    ''')


class EmitLauncher(ws_case.RecorderLauncher):
    """The recorder terminals, plus an emitter script: after ``GO`` it writes ``emit.bin`` to the PTY."""

    def __post_init__(self):
        super().__post_init__()
        self.emitter = self.base / 'emitter.py'
        self.emitter.write_text(EMITTER)
        (self.base / 'emit.bin').write_bytes(b'')

    def started(self, name: str):
        started = super().started(name)
        started.argv = [started.argv[0], '-I', str(self.emitter), started.argv[3], name, str(self.base / 'emit.bin')]
        return started


class DrainedTab:
    """Keeps reading a tab's frames in the background so the Hub's per-connection send queue never fills."""

    def __init__(self, tab):
        self.tab = tab
        self.task = asyncio.ensure_future(self._run())

    async def _run(self):
        while True:
            try:
                if await self.tab._next(5) is None:
                    return
            except asyncio.TimeoutError:
                continue

    async def stop(self):
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass


@unittest.skipUnless(HAS_NODE, 'node is not installed')
class InputEndToEndTests(ws_case.BoardWsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.board.launcher = self.launcher = EmitLauncher(self.base / 'emit', state_dir=self.state)
        self.emit = self.launcher.base / 'emit.bin'

    async def attached(self, drained: bool = True):
        """agent-1 and a shell, one connection each (v2); ``ids`` maps the v1 channel names to terminal ids."""
        self.ids = {'agent': await self.agent(), 'shell': await self.shell()}
        tabs = {}
        for name, terminal in self.ids.items():
            tab = tabs[name] = self.tab(terminal)
            await tab.attach(self.session.csrf)
            await tab.wait_output(b'ready:')
            if drained:
                drain = DrainedTab(tab)
                self.addAsyncCleanup(drain.stop)
        return tabs

    async def test_random_mixed_input_split_by_the_real_page_code_arrives_byte_exact_and_in_order(self):
        tabs = await self.attached()
        expected = {'agent': b'', 'shell': b''}
        sizes = [1, 2, 4, 64, FRAME_LIMIT - 3, FRAME_LIMIT - 1, FRAME_LIMIT, FRAME_LIMIT + 1, FRAME_LIMIT + 2,
                 FRAME_LIMIT + 3, 2 * FRAME_LIMIT + 5, 1 << 20]
        for index, size in enumerate(sizes):
            channel = 'shell' if index % 2 == 0 else 'agent'
            data = mixed_bytes(1000 + index, size)
            pieces = node_split(self, data)
            self.assertEqual(b''.join(pieces), data)
            for piece in pieces:
                self.assertLessEqual(len(piece), FRAME_LIMIT)
                piece.decode('utf-8')
                await tabs[channel].send_input(piece)
            expected[channel] += data
            for name, want in expected.items():
                terminal = self.ids[name]
                self.assertTrue(await self.eventually(lambda: len(self.recorded(terminal)) >= len(want), 30),
                                f'size {size} on {name}: {len(self.recorded(terminal))} of {len(want)} bytes')
                self.assertEqual(self.recorded(terminal), want, f'size {size} on {name}')
        # v1: no dropped input and the same epoch; v2: no error and both connections still attached
        for tab in tabs.values():
            self.assertNotIn('error', [event.get('t') for event in tab.events])
            self.assertIsNone(tab.close_code)

    async def test_the_echo_of_typed_hangul_comes_back_whole(self):
        tabs = await self.attached()
        typed = '안녕하세요 ㄱㅏ 한 ㅤ 👨‍👩‍👧 中文'.encode()
        await tabs['shell'].send_input(typed)
        self.assertTrue(await self.eventually(lambda: b'<' + typed + b'>' in bytes(tabs['shell'].output)))
        self.assertEqual(self.recorded(self.ids['shell']), typed)

    async def test_a_4_mib_input_is_taken_whole_and_one_byte_more_is_refused_whole(self):
        await self.attached()
        shell = self.ids['shell']
        exact = mixed_bytes(5, QUEUE_LIMIT)
        self.assertTrue(self.board.write(shell, exact))
        self.assertTrue(await self.eventually(lambda: len(self.recorded(shell)) >= QUEUE_LIMIT, 40))
        self.assertEqual(self.recorded(shell), exact)
        over = mixed_bytes(6, QUEUE_LIMIT + 1)
        self.assertFalse(self.board.write(shell, over), 'refused, not split')
        await asyncio.sleep(1.0)
        self.assertEqual(self.recorded(shell), exact, 'nothing of the refused input was delivered')
        # the terminal is still fine afterwards and sees no leftover of the refused input
        self.assertTrue(self.board.write(shell, 'ok 한글'.encode()))
        self.assertTrue(await self.eventually(lambda: self.recorded(shell).endswith('ok 한글'.encode())))
        self.assertEqual(self.recorded(shell), exact + 'ok 한글'.encode())

    async def test_a_4_mib_paste_in_the_pages_frames_is_delivered_whole_through_the_websocket(self):
        tabs = await self.attached()
        shell = self.ids['shell']
        data = mixed_bytes(9, QUEUE_LIMIT)
        pieces = node_split(self, data)
        self.assertEqual(b''.join(pieces), data)
        for piece in pieces:
            await tabs['shell'].send_input(piece)
        self.assertTrue(await self.eventually(lambda: len(self.recorded(shell)) >= len(data), 40))
        self.assertEqual(self.recorded(shell), data)
        self.assertNotIn('error', [event.get('t') for event in tabs['shell'].events])  # v1: dropped_inputs == 0

    async def test_output_is_byte_exact_live_and_a_late_tab_gets_a_replay_from_a_boundary_with_the_modes(self):
        model = build_model(2026, int(RING_BYTES * 1.3))
        self.emit.write_bytes(model.stream)
        tabs = await self.attached(drained=False)
        tab = tabs['shell']
        label = self.records[self.ids['shell']].stem.encode()  # the recorder's name of this shell
        banner = bytes(tab.output)
        self.assertEqual(banner, b'ready:' + label + b'\r\n')
        await tab.send_input(b'GO')
        done = b'done:' + label + b'\r\n'
        want = len(banner) + len(model.stream) + len(done)
        await tab.wait_output(done, 60)
        self.assertEqual(len(tab.output), want)
        live = bytes(tab.output)
        self.assertEqual(live[len(banner):len(banner) + len(model.stream)], model.stream, 'live output is byte-exact')
        self.assertGreater(len(model.stream), RING_BYTES)
        late = self.tab(self.ids['shell'])
        await late.attach(self.session.csrf)
        await late.wait_output(done, 30)
        replay = bytes(late.output)
        self.assertTrue(len(late.frames) > 1, 'the replay was sent in several frames')
        whole = live  # banner + stream + done marker: what the ring was fed
        stream_cut = None
        for modes, rest in replay_splits(replay):
            cut = len(whole) - len(rest) - len(banner)  # the replay start as an offset in the model's stream
            if (whole.endswith(rest) and 0 < cut <= len(model.stream) and model.valid[cut]
                    and modes == set(model.state_at(cut) & PAGE_MODES)):
                stream_cut = cut
                break
        self.assertIsNotNone(stream_cut, f'no reading of the replay is right; head {replay[:60]!r}')
        codecs.getincrementaldecoder('utf-8')('strict').decode(rest, final=True)
        self.assertGreaterEqual(len(rest), RING_BYTES - model.max_token - 3)
        self.assertLessEqual(len(rest), RING_BYTES + model.max_token)
        # whatever the frame boundaries were, a streaming decode of the frames reconstructs the text
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        text = ''.join(decoder.decode(payload) for payload in late.frames)
        self.assertEqual(text.encode('utf-8'), replay)


# ---------------------------------------------------------------------------------------------------------------
# Locale and line discipline of the real shell channel.

LOCALE_KEYS = ('LANG', 'LC_ALL', 'LC_CTYPE')


def effective_ctype(env: dict) -> str | None:
    """POSIX precedence for the character type: LC_ALL, then LC_CTYPE, then LANG (first non-empty)."""
    for key in ('LC_ALL', 'LC_CTYPE', 'LANG'):
        if env.get(key):
            return env[key]
    return None


def is_utf8(name: str | None) -> bool:
    return name is not None and re.search(r'utf-?8', name, re.IGNORECASE) is not None


LOCALE_CASES = [
    ('no locale', {}),
    ('LANG=C', {'LANG': 'C'}),
    ('LANG=POSIX', {'LANG': 'POSIX'}),
    ('empty LANG', {'LANG': ''}),
    ('LANG=en_US.ISO-8859-1', {'LANG': 'en_US.ISO-8859-1'}),
    ('LANG=ko_KR.EUC-KR', {'LANG': 'ko_KR.EUC-KR'}),
    ('LC_ALL=C beats a UTF-8 LANG', {'LANG': 'ko_KR.utf8', 'LC_ALL': 'C'}),
    ('LC_CTYPE=POSIX beats a UTF-8 LANG', {'LANG': 'ko_KR.utf8', 'LC_CTYPE': 'POSIX'}),
    ('LC_ALL empty, LC_CTYPE=C', {'LC_ALL': '', 'LC_CTYPE': 'C'}),
    ('user LANG=ko_KR.utf8', {'LANG': 'ko_KR.utf8'}),
    ('user LANG=en_US.UTF-8', {'LANG': 'en_US.UTF-8'}),
    ('user LC_ALL=ko_KR.UTF-8 over LANG=C', {'LANG': 'C', 'LC_ALL': 'ko_KR.UTF-8'}),
    ('user LC_CTYPE=en_US.utf8 over LANG=C', {'LANG': 'C', 'LC_CTYPE': 'en_US.utf8'}),
]


class LocaleEnvironmentTests(launch_case.LauncherCase):
    def environments(self, given: dict) -> list[tuple[str, dict]]:
        for key in LOCALE_KEYS:
            self.box.env.pop(key, None)
        self.box.env.update(given)
        launcher = self.make_launcher(self.config)
        return [('shell', launcher.prepare_shell().env), ('agent', self.prepare(launcher).env)]

    def test_both_channels_get_an_effective_utf8_character_type_and_a_users_utf8_locale_is_kept(self):
        for label, given in LOCALE_CASES:
            for channel, env in self.environments(given):
                with self.subTest(case=label, channel=channel):
                    self.assertTrue(is_utf8(effective_ctype(env)), {k: env.get(k) for k in LOCALE_KEYS})
                    if is_utf8(effective_ctype(given)):
                        self.assertEqual({k: env.get(k) for k in LOCALE_KEYS}, {k: given.get(k) for k in LOCALE_KEYS},
                                         'the user\'s own UTF-8 locale is not rewritten')

    def test_the_environment_really_gives_a_utf8_charmap_to_a_process(self):
        for label, given in LOCALE_CASES:
            for channel, env in self.environments(given):
                with self.subTest(case=label, channel=channel):
                    done = subprocess.run(['locale', 'charmap'], env={'PATH': '/usr/bin:/bin', **env},
                                          capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
                    self.assertEqual(done.stdout.strip(), 'UTF-8')


class ShellChannelTests(set_case.SetCase):
    def setUp(self):
        super().setUp()
        for key in LOCALE_KEYS:
            self.box.env.pop(key, None)

    def run_shell(self, shell: str, typed: bytes, needle: bytes, **env) -> bytes:
        self.box.env.update(env)
        self.box.env['SHELL'] = shell
        self.launcher = self.make_launcher(self.config)
        self.board = self.make_board()
        self.start()
        self.assertTrue(self.board.write(self.tid('shell'), typed))
        self.assertTrue(self.wait(lambda: needle in self.board.replay(self.tid('shell'))),
                        self.board.replay(self.tid('shell'))[-400:])
        return self.board.replay(self.tid('shell'))

    def test_the_effective_charmap_is_utf8_without_a_locale_with_lang_c_and_with_a_users_utf8_locale(self):
        installed = subprocess.run(['locale', '-a'], capture_output=True, text=True).stdout.split()
        cases = [({}, 'no locale'), ({'LANG': 'C'}, 'LANG=C'), ({'LANG': 'POSIX', 'LC_ALL': 'C'}, 'LC_ALL=C')]
        if 'ko_KR.utf8' in installed:
            cases.append(({'LANG': 'ko_KR.utf8'}, 'user ko_KR.utf8'))
        for env, label in cases:
            with self.subTest(label):
                for key in LOCALE_KEYS:
                    self.box.env.pop(key, None)
                replay = self.run_shell('/bin/sh', b'echo "<<$(locale charmap)>>"\r', b'>>\r\n', **env)
                self.assertIn(b'<<UTF-8>>', replay, replay[-300:])
                self.arun(self.board.stop())
                self.board = self.make_board()

    def test_the_pty_has_iutf8_and_a_cooked_mode_line_erases_whole_hangul_syllables(self):
        self.run_shell('/bin/sh', b'stty -a\r', b'isig')
        self.assertRegex(self.board.replay(self.tid('shell')), rb'(?<!-)iutf8')
        typed = 'read v\r한글가나'.encode() + b'\x7f\x7f' + '다\rprintf "<%s>\\n" "$v"\r'.encode()
        self.board.write(self.tid('shell'), typed)
        self.assertTrue(self.wait(lambda: '<한글다>'.encode() in self.board.replay(self.tid('shell'))),
                        self.board.replay(self.tid('shell'))[-400:])

    @unittest.skipUnless(shutil.which('bash'), 'bash is not installed')
    def test_bash_readline_backspace_removes_one_whole_character_for_syllables_and_emoji(self):
        typed = ('printf "[%s]\\n" "한글가' .encode() + b'\x7f\x7f' + '나😀'.encode() + b'\x7f' + '다"\r'.encode())
        replay = self.run_shell(shutil.which('bash'), typed, '다]'.encode())
        self.assertIn('[한나다]'.encode(), replay, replay[-400:])
        # a Hangul line typed and left-arrowed over: the cursor moves by characters, not bytes
        self.board.write(self.tid('shell'), 'printf "[%s]\\n" "가나다'.encode() + b'\x1b[D\x1b[D' + b'X"\r')
        self.assertTrue(self.wait(lambda: '[가X나다]'.encode() in self.board.replay(self.tid('shell'))),
                        self.board.replay(self.tid('shell'))[-400:])


# ---------------------------------------------------------------------------------------------------------------
# Web pages.

TITLE_MAX = 200
BODY_MAX = 64 * 1024
COMMENT_MAX = 16 * 1024


def hangul_filled(size: int, tail: str = 'a') -> str:
    """Hangul syllables filling ``size`` UTF-8 bytes; the remainder (0-2 bytes) is ``tail`` characters."""
    text = '한' * (size // 3) + tail * (size % 3)
    assert len(text.encode('utf-8')) == size
    return text


class WebHangulTests(request_case.RequestsCase):
    async def create(self, title: str, body: str, *, multipart: bool):
        form = (await self.page('/requests/new'))[1].form('/requests')
        fields = {'csrf': form['fields']['csrf'], 'project': self.a, 'nonce': form['fields']['nonce'],
                  'title': title, 'body': body}
        if not multipart:
            return await self.post('/requests', fields, cookies=self.cookies)
        data = aiohttp.FormData()
        for name, value in fields.items():
            data.add_field(name, value, content_type='text/plain; charset=utf-8')
        return await self.post('/requests', data, cookies=self.cookies)

    async def comment(self, request_id: str, body: str, *, multipart: bool):
        csrf = await self.csrf()
        path = f'/requests/{request_id}/comments'
        if not multipart:
            return await self.post(path, {'csrf': csrf, 'body': body}, cookies=self.cookies)
        data = aiohttp.FormData()
        data.add_field('csrf', csrf, content_type='text/plain; charset=utf-8')
        data.add_field('body', body, content_type='text/plain; charset=utf-8')
        return await self.post(path, data, cookies=self.cookies)

    def stored(self) -> list[tuple]:
        try:
            return self.db_rows('SELECT title, body FROM requests ORDER BY seq')
        except sqlite3.OperationalError:  # no request was ever stored: the project database does not exist yet
            return []

    def stored_comments(self, request_id: str) -> list[str]:
        return [row[0] for row in self.db_rows('SELECT body FROM comments WHERE request_id = ? ORDER BY seq', request_id)]

    async def test_random_hangul_titles_bodies_and_comments_round_trip_exactly_in_both_encodings(self):
        rand = random.Random(11)
        request_id = self.add('시작', '시작 본문')
        pool = [_syllable, lambda r: unicodedata.normalize('NFD', _syllable(r)), lambda r: chr(r.randint(0x3131, 0x318E)),
                lambda r: chr(r.randint(0x1100, 0x11FF)), lambda r: r.choice(['😀', '👍🏽', '🇰🇷']),
                lambda r: chr(r.randint(0x4E00, 0x9FFF)), lambda r: chr(r.randint(0x21, 0x7E)), lambda r: ' ']
        for round_ in range(8):
            multipart = round_ % 2 == 1
            title = ''.join(rand.choice(pool)(rand) for _ in range(rand.randint(1, 60))).strip() or '가'
            body = ''.join(rand.choice(pool + [lambda r: '\n'])(rand) for _ in range(rand.randint(1, 400)))
            with self.subTest(round=round_, multipart=multipart):
                response = await self.create(title, body, multipart=multipart)
                self.assertEqual(response.status, 303, response.body_text[:400])
                self.assertEqual(self.stored()[-1], (title, body))
                _, shown = await self.page(response.headers['Location'])
                self.assertIn(title, shown.body_text)
                self.assertEqual(shown.pre[0], body)
                reply = await self.comment(request_id, body, multipart=multipart)
                self.assertEqual(reply.status, 303, reply.body_text[:400])
                self.assertEqual(self.stored_comments(request_id)[-1], body)

    async def test_the_title_limit_counts_code_points_not_bytes_utf16_units_or_normalized_forms(self):
        cases = [
            ('200 syllables', '한' * TITLE_MAX, True),
            ('201 syllables', '한' * (TITLE_MAX + 1), False),
            ('200 code points with 100 emoji (400 UTF-16 units)', '😀' * 100 + '글' * 100, True),
            ('201 code points with emoji', '😀' * 101 + '글' * 100, False),
            ('100 decomposed LV pairs = 200 code points', unicodedata.normalize('NFD', '하') * 100, True),
            ('101 decomposed pairs = 202 code points', unicodedata.normalize('NFD', '하') * 101, False),
            ('200 compatibility jamo', 'ㄱ' * TITLE_MAX, True),
        ]
        for multipart in (False, True):
            for label, title, accepted in cases:
                with self.subTest(label=label, multipart=multipart):
                    before = len(self.stored())
                    response = await self.create(title, '본문', multipart=multipart)
                    if accepted:
                        self.assertEqual(response.status, 303, response.body_text[:300])
                        self.assertEqual(self.stored()[-1][0], title, 'stored exactly, not normalized')
                    else:
                        self.assertEqual(response.status, 400)
                        self.assertEqual(len(self.stored()), before, 'a refused title stores nothing')
                        self.assertIn(title, response.body_text, 'the typed text comes back in the form')

    async def test_body_and_comment_limits_count_utf8_bytes_at_the_exact_hangul_boundary(self):
        request_id = self.add('경계', '본문')
        for multipart in (False, True):
            with self.subTest(multipart=multipart):
                exact = hangul_filled(BODY_MAX, 'a')
                over_by_one = '한' * (BODY_MAX // 3) + 'ab'  # 65538 bytes
                one_syllable_over = hangul_filled(BODY_MAX - 2, 'a') + '한'  # 65537 bytes
                response = await self.create('본문 경계', exact, multipart=multipart)
                self.assertEqual(response.status, 303, response.body_text[:200])
                self.assertEqual(self.stored()[-1][1], exact)
                count = len(self.stored())
                for over in (over_by_one, one_syllable_over):
                    response = await self.create('본문 초과', over, multipart=multipart)
                    self.assertEqual(response.status, 400)
                    self.assertEqual(len(self.stored()), count)
                exact_comment = hangul_filled(COMMENT_MAX, 'a')
                response = await self.comment(request_id, exact_comment, multipart=multipart)
                self.assertEqual(response.status, 303, response.body_text[:200])
                done = len(self.stored_comments(request_id))
                for over in ('한' * (COMMENT_MAX // 3) + 'ab', hangul_filled(COMMENT_MAX - 2, 'a') + '한'):
                    response = await self.comment(request_id, over, multipart=multipart)
                    self.assertEqual(response.status, 400)
                self.assertEqual(len(self.stored_comments(request_id)), done)

    async def test_no_text_field_of_the_request_pages_has_a_maxlength_that_counts_utf16_units(self):
        request_id = self.add('제목', '본문')
        offenders = []
        for path in ('/requests', '/requests/new', f'/requests/{request_id}', f'/requests/{request_id}/edit'):
            _, page = await self.page(path)
            for tag in ('input', 'textarea'):
                for attrs in page.tags(tag):
                    if attrs.get('type') in ('hidden', 'checkbox', 'submit', 'radio'):
                        continue
                    if 'maxlength' in attrs:
                        offenders.append((path, tag, attrs.get('name'), attrs['maxlength']))
        self.assertEqual(offenders, [], 'HTML maxlength counts UTF-16 units and cuts IME input')

    async def test_search_matches_composed_and_decomposed_hangul_in_both_directions_and_in_the_body(self):
        nfc, nfd = '검색 대상 문서', unicodedata.normalize('NFD', '분해된 제목')
        self.add(nfc, '본문 하나')
        self.add(nfd, unicodedata.normalize('NFD', '분해된 본문 내용'))
        self.add('Plain English Title', 'body')
        both = lambda text: (text, unicodedata.normalize('NFC', text), unicodedata.normalize('NFD', text))
        for query, expected in [(q, nfc) for q in both('검색')] + [(q, nfd) for q in both('분해된')] + \
                [(q, nfd) for q in both('본문 내용')] + [(q, nfc) for q in both('본문 하나')]:
            with self.subTest(query=ascii(query)):
                _, page = await self.page('/requests?q=' + quote(query))
                titles = [row[2] for row in page.data_rows()]
                self.assertEqual(titles, [expected], titles)
        _, page = await self.page('/requests?q=' + quote('PLAIN english'))
        self.assertEqual([row[2] for row in page.data_rows()], ['Plain English Title'])
        _, page = await self.page('/requests?q=' + quote('없는말'))
        self.assertEqual(page.data_rows(), [])

    async def test_a_200_character_hangul_query_is_accepted(self):
        long_title = '가' * 100 + '나' * 100
        self.add(long_title, '본문')
        response, page = await self.page('/requests?q=' + quote(long_title))
        self.assertEqual(response.status, 200)
        self.assertEqual([row[2] for row in page.data_rows()], [long_title])
        response, page = await self.page('/requests?q=' + quote(long_title + '다'))
        self.assertEqual(response.status, 400, 'a 201-character query is refused, visibly')
        self.assertEqual(page.data_rows(), [])


if __name__ == '__main__':
    unittest.main()
