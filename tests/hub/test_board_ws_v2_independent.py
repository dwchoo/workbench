"""WBX-D1 part 2, second independent pass: WebSocket protocol v2 shared attach under load (seam T5/S1).

Expected values come from web-board-ux SPEC s4 "WebSocket v2: terminal마다 공유 연결" (identical output for every
client, replay then live without gap or overlap, input in arrival order, size owner rule, 8 clients -> 4429, per
connection queue/heartbeat/paste backpressure, 4404/1001 for everyone, caps 128 / 8 pending, message validation,
per-connection audit), BRIEF WBX-D54/D55 and AC-32/33/34; not from the product or its own tests. This file adds what
the first pass (``test_board_ws_independent.py``) did not push: byte-for-byte comparison of 2..8 clients under tens of
MB of numbered output, per-client ordering of concurrent input frames (a header per frame, whole-frame parsing of the
recorder's file), Korean/UTF-8 split across frames, the 9th client under load, size-message counts and
size-before-bytes, one slow client against 3 fast ones, paste backpressure with a non-reading program, the real 128 /
8 caps, and scripted audit rows.

The program on each PTY is ``SCRIPT`` below (raw mode): it records every input byte to ``<label>.stdin``, and answers
``SIZE`` / ``GO<lines>,<batch>,<pause_us>;`` (numbered output lines ``N0000000\\r\\n`` ... then ``DONE\\r\\n``); marker
files ``<label>.hold`` (do not read stdin while it exists), ``<label>.quiet`` (no echo) and ``<label>.go`` (start
output at once) are made before the terminal starts. Loopback only, temp HOME/XDG, every PTY is checked gone (pid +
start ticks) at the end.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import aiohttp
from aiohttp import web

from support.board_stub import RecorderLauncher
from support.ws_client import Tab

from workbench.hub import auth
from workbench.hub.board import ws as board_ws
from workbench.hub.board.set import BoardSet
from workbench.hub.web.app import WEBSOCKETS, OriginPolicy, WebServices, create_app

PASSWORD = 'correct horse battery'

SCRIPT = textwrap.dedent('''\
    import os, re, sys, threading, time, tty
    COMMAND = re.compile(rb"GO(\\d+,\\d+,\\d+;)")
    record, label = sys.argv[1], sys.argv[2]
    stem = record[:-len(".stdin")]
    hold, quiet, go = stem + ".hold", os.path.exists(stem + ".quiet"), stem + ".go"
    try:
        tty.setraw(0)
    except Exception:
        pass
    os.write(1, b"ready:" + label.encode() + b"\\r\\n")
    out = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

    def ticker(lines, batch, pause):
        n = 0
        while n < lines:
            k = min(batch, lines - n)
            os.write(1, b"".join(b"N%07d\\r\\n" % (n + i) for i in range(k)))
            n += k
            if pause:
                time.sleep(pause)
        os.write(1, b"DONE\\r\\n")

    def start(spec):
        lines, batch, pause_us = (int(x) for x in spec.split(b";")[0].split(b","))
        threading.Thread(target=ticker, args=(lines, batch, pause_us / 1e6), daemon=True).start()

    if os.path.exists(go):
        start(open(go, "rb").read())
    while os.path.exists(hold):
        time.sleep(0.01)
    while True:
        data = os.read(0, 65536)
        if not data:
            break
        os.write(out, data)
        if data.startswith(b"SIZE"):
            size = os.get_terminal_size(0)
            os.write(1, b"size:%d,%d;" % (size.lines, size.columns))
        elif COMMAND.match(data):  # (a random paste that happens to begin with "GO" is plain input)
            start(COMMAND.match(data).group(1))
        elif not quiet:
            os.write(1, b"<" + data + b">")
    ''')


def ticks(pid: int) -> str | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def alive(pid: int, start: str | None) -> bool:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[19] == start and fields[0] not in ('Z', 'X')


async def until(predicate, timeout: float = 10.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return bool(predicate())
        await asyncio.sleep(step)


def numbered(first: int, end: int) -> bytes:
    return b''.join(b'N%07d\r\n' % index for index in range(first, end))


def full_stream(label: str, lines: int) -> bytes:
    return b'ready:' + label.encode() + b'\r\n' + numbered(0, lines) + b'DONE\r\n'


def full_digest(label: str, lines: int) -> tuple[str, int]:
    """sha256 and length of ``full_stream`` built in pieces (no 40 MB string)."""
    digest = hashlib.sha256()
    digest.update(b'ready:' + label.encode() + b'\r\n')
    total = len(b'ready:' + label.encode() + b'\r\n')
    for first in range(0, lines, 50000):
        piece = numbered(first, min(first + 50000, lines))
        digest.update(piece)
        total += len(piece)
    digest.update(b'DONE\r\n')
    return digest.hexdigest(), total + 6


class Sink:
    """Reads one attached ``Tab`` in the background: events (dicts, in order), a running sha256 and length of the
    data frames, the bytes themselves when ``keep``, the largest gap between data frames and the close code."""

    def __init__(self, tab: Tab, attached: dict, keep: bool = True):
        self.tab = tab
        self.attached = attached
        self.events = list(tab.events)
        self.hash = hashlib.sha256()
        self.length = 0
        self.data = bytearray() if keep else None
        self.keep = keep
        self.close_code: int | None = None
        self.last_data: float | None = None
        self.max_gap = 0.0
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        ws = self.tab.ws
        while True:
            try:
                message = await ws.receive()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.close_code = self.close_code or 1006
                return
            if message.type == aiohttp.WSMsgType.BINARY:
                now = time.monotonic()
                if self.last_data is not None:
                    self.max_gap = max(self.max_gap, now - self.last_data)
                self.last_data = now
                self.hash.update(message.data)
                self.length += len(message.data)
                if self.keep:
                    self.data += message.data
            elif message.type == aiohttp.WSMsgType.TEXT:
                self.events.append(json.loads(message.data))
            else:
                self.close_code = ws.close_code if ws.close_code is not None else 1006
                return

    def of(self, kind: str) -> list[dict]:
        return [event for event in self.events if event.get('t') == kind]

    async def send(self, message: dict) -> None:
        await self.tab.send(message)

    async def type(self, payload: bytes) -> None:
        await self.tab.send_input(payload)

    async def wait_bytes(self, needle: bytes, timeout: float = 10.0) -> bool:
        return await until(lambda: needle in self.data, timeout)

    async def wait_event(self, kind: str, count: int = 1, timeout: float = 10.0) -> bool:
        return await until(lambda: len(self.of(kind)) >= count, timeout)

    async def wait_closed(self, timeout: float = 10.0) -> int | None:
        await until(lambda: self.close_code is not None, timeout)
        return self.close_code

    @property
    def digest(self) -> tuple[str, int]:
        return self.hash.hexdigest(), self.length


class Env(unittest.IsolatedAsyncioTestCase):
    HEARTBEAT = 30.0  # quiet by default: Python clients under load must not be timed out by accident
    SLACK = 1.0
    HELLO = 5.0
    QUEUE_LIMIT = board_ws.SEND_QUEUE_LIMIT
    TERMINAL_KWARGS: dict = {}

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-v2i-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        runtime = Path(tempfile.mkdtemp(prefix='wx'))
        runtime.chmod(0o700)
        self.addCleanup(shutil.rmtree, runtime, True)
        patch = mock.patch.dict(os.environ, {
            'HOME': str(self.base), 'XDG_CONFIG_HOME': str(self.base / 'cfg'), 'XDG_STATE_HOME': str(self.base / 'st'),
            'XDG_DATA_HOME': str(self.base / 'data'), 'XDG_CACHE_HOME': str(self.base / 'cache'),
            'XDG_RUNTIME_DIR': str(runtime), 'WORKBENCH_HUB_AUTOSTART': '0'})
        patch.start()
        self.addCleanup(patch.stop)
        self.state = self.base / 'hub'
        self.passwords = auth.PasswordStore(self.state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(self.state)
        self.addCleanup(self.db.close)
        self.services = WebServices(self.passwords, self.db, OriginPolicy('127.0.0.1'))
        self.launcher = RecorderLauncher(self.base / 'terminals', state_dir=self.state)
        self.launcher.script.write_text(SCRIPT)
        self.board = BoardSet(self.launcher, grace=1.0)
        self.terminal = board_ws.BoardTerminal(
            self.board, heartbeat=self.HEARTBEAT, slack=self.SLACK, hello_timeout=self.HELLO,
            queue_limit=self.QUEUE_LIMIT, audit=board_ws.audit_to(self.services), **self.TERMINAL_KWARGS)
        self.app = create_app(self.services, routes=[board_ws.routes(lambda: self.terminal)])
        self.runner = web.AppRunner(self.app, access_log=None, handle_signals=False, shutdown_timeout=2)
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', 0).start()
        self.port = self.runner.addresses[0][1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.tabs: list[Tab] = []
        self.sinks: list[Sink] = []
        self.records: dict[str, Path] = {}
        self.labels: dict[str, str] = {}
        self.procs: dict[int, str | None] = {}
        self.session = self.login()
        self.addAsyncCleanup(self.shutdown)

    def login(self, address: str = '127.0.0.1'):
        return self.services.sessions.create(address, 'test', time.time(), generation=self.generation)

    def remember(self) -> None:
        for view in self.board.views():
            if view.get('pid'):
                self.procs.setdefault(view['pid'], ticks(view['pid']))

    async def shutdown(self):
        self.remember()
        for sink in self.sinks:
            sink.task.cancel()
        await asyncio.gather(*(sink.task for sink in self.sinks), return_exceptions=True)
        for tab in self.tabs:
            await tab.close()
        await self.runner.cleanup()
        await self.board.stop()
        leaked = [pid for pid, start in self.procs.items() if alive(pid, start)]
        for pid in leaked:  # identity matched: ours
            os.kill(pid, 9)
        self.assertEqual(leaked, [], 'PTY processes outlived the test')

    # --- terminals ----------------------------------------------------------------------------------------

    def markers(self, label: str, *, hold: bool, quiet: bool, go: str | None) -> None:
        stem = self.launcher.base / label
        if hold:
            stem.with_suffix('.hold').write_text('')
        if quiet:
            stem.with_suffix('.quiet').write_text('')
        if go:
            stem.with_suffix('.go').write_text(go)

    async def shell(self, *, hold: bool = False, quiet: bool = False, go: str | None = None) -> str:
        label = f'shell{self.launcher.shells + 1}'
        self.markers(label, hold=hold, quiet=quiet, go=go)
        view = await self.board.create_shell()
        self.records[view['id']] = self.launcher.base / f'{label}.stdin'
        self.labels[view['id']] = label
        self.remember()
        self.assertTrue(await until(lambda: b'ready:' in self.board.replay(view['id']), 10))
        return view['id']

    async def agent(self, *, quiet: bool = False) -> str:
        agent_id = self.board.views()[0]['id']
        self.markers(agent_id, hold=False, quiet=quiet, go=None)
        await self.board.open([agent_id])
        await self.board.settled()
        self.records[agent_id] = self.launcher.base / f'{agent_id}.stdin'
        self.labels[agent_id] = agent_id
        self.remember()
        self.assertTrue(await until(lambda: b'ready:' in self.board.replay(agent_id), 10))
        return agent_id

    def recorded(self, terminal_id: str) -> bytes:
        try:
            return self.records[terminal_id].read_bytes()
        except FileNotFoundError:
            return b''

    def diag(self, terminal_id: str) -> str:
        """What a stalled test needs to be classified: the Hub's view of every connection and the PTY input queue."""
        rows = [f'client={c.client} closing={c.closing} paused={c.paused} backpressure={c.backpressure} '
                f'queued={c.queued}' for c in sorted(self.terminal.connections, key=lambda c: c.client)]
        sinks = [f'close={sink.close_code} errors={len(sink.of("error"))}' for sink in self.sinks]
        return (f'recorded={len(self.recorded(terminal_id))} backlog={self.board.input_backlog(terminal_id)} '
                f'view={ {k: v for k, v in self.board.view(terminal_id).items() if k in ("state", "exit_status", "detail")} } '
                f'tail={self.board.replay(terminal_id)[-300:]!r} connections=[{"; ".join(rows)}] sinks=[{"; ".join(sinks)}]')

    def release_hold(self, terminal_id: str) -> None:
        self.records[terminal_id].with_suffix('.hold').unlink()

    # --- clients ----------------------------------------------------------------------------------------

    async def join(self, terminal_id: str, session=None, *, size=(30, 100), keep: bool = True) -> Sink:
        session = session or self.session
        tab = Tab(self.url, session.token, terminal_id)
        self.tabs.append(tab)
        attached = await tab.attach(session.csrf, size=size)
        sink = Sink(tab, attached, keep)
        self.sinks.append(sink)
        return sink

    async def raw(self, terminal_id: str, session=None) -> Tab:
        """A connected tab (no hello yet), not read in the background."""
        session = session or self.session
        tab = Tab(self.url, session.token, terminal_id)
        self.tabs.append(tab)
        await tab.connect()
        return tab

    def audit(self, prefix: str = 'terminal_') -> list[tuple[str, dict]]:
        db = sqlite3.connect(f'file:{self.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT event, detail_json FROM audit WHERE event LIKE ? ORDER BY id',
                              (prefix + '%',)).fetchall()
        finally:
            db.close()
        return [(event, json.loads(detail)) for event, detail in rows]

    async def audited(self, event: str, count: int, timeout: float = 10.0) -> list[dict]:
        """The details of the ``event`` rows once there are ``count`` of them (rows are written by an executor, in
        order, after the terminal has moved on)."""
        await until(lambda: len([1 for e, _ in self.audit() if e == event]) >= count, timeout)
        return [detail for e, detail in self.audit() if e == event]

    async def size_of(self, sink: Sink) -> str:
        """What the program sees as its window size when ``sink`` types (the answer ``size:r,c;`` it prompts)."""
        before = sink.data.count(b'size:')
        await sink.type(b'SIZE')
        self.assertTrue(await until(lambda: sink.data.count(b'size:') > before and sink.data.endswith(b';'), 10))
        tail = bytes(sink.data)
        return tail[tail.rindex(b'size:'):].decode()


# =========================================================================================================
# 1. identical output, replay-live boundary
# =========================================================================================================

class IdenticalOutputTests(Env):
    LINES = 400_000  # 3.6 MB of numbered lines, several times the 1 MiB ring

    async def run_flood(self, clients: int) -> None:
        shell = await self.shell(quiet=True)
        label = self.labels[shell]
        sessions = [self.session] + [self.login() for _ in range(clients // 8)]
        sinks = [await self.join(shell, sessions[index // 8], keep=False) for index in range(clients)]
        await sinks[0].type(b'GO%d,400,0;' % self.LINES)
        expected = full_digest(label, self.LINES)
        self.assertTrue(await until(lambda: all(sink.length >= expected[1] for sink in sinks), 120),
                        [sink.length for sink in sinks])
        await asyncio.sleep(0.3)
        for index, sink in enumerate(sinks):
            self.assertEqual(sink.digest, expected, f'client {index} of {clients} saw different bytes')
            self.assertIsNone(sink.close_code)

    async def test_two_clients_see_the_identical_numbered_stream(self):
        await self.run_flood(2)

    async def test_five_clients_see_the_identical_numbered_stream(self):
        await self.run_flood(5)

    async def test_eight_clients_see_the_identical_numbered_stream(self):
        await self.run_flood(8)

    async def test_clients_attaching_while_the_flood_runs_get_an_exact_suffix_of_it(self):
        """Replay (the ring so far) then live: the sum is always a suffix of the whole stream, i.e. no chunk is lost
        at the boundary and none is delivered twice."""
        shell = await self.shell(quiet=True)
        label = self.labels[shell]
        first = await self.join(shell, keep=False)
        await first.type(b'GO%d,100,1000;' % self.LINES)  # about 4 s of flow: the attaches land mid-stream
        late: list[Sink] = []
        for _ in range(6):
            await asyncio.sleep(0.15)
            late.append(await self.join(shell, keep=True))
            self.assertLess(first.length, full_digest(label, self.LINES)[1], 'the flood was still running')
        expected = full_stream(label, self.LINES)
        self.assertTrue(await until(lambda: all(sink.data.endswith(b'DONE\r\n') for sink in late), 120))
        for index, sink in enumerate(late):
            self.assertTrue(expected.endswith(bytes(sink.data)), f'late client {index}: not a suffix of the stream '
                                                                f'(length {len(sink.data)})')
            self.assertGreaterEqual(len(sink.data), 1 << 18, 'the replay is the ring tail, not nothing')
        self.assertEqual(first.digest, full_digest(label, self.LINES))

    async def test_attach_storm_during_a_slow_tick_all_equal_the_whole_stream(self):
        """Output under 1 MiB keeps the whole history in the ring: every attacher, at any moment, ends with exactly
        the full stream (replay then live, no gap, no duplicate)."""
        lines = 60_000
        shell = await self.shell(quiet=True, go=f'{lines},100,2000;')
        label = self.labels[shell]
        expected = full_stream(label, lines)
        sinks: list[Sink] = []
        sessions = [self.login() for _ in range(3)]
        for wave, count in enumerate((3, 3, 2)):  # 8 clients in all, in waves of simultaneous hellos
            sinks.extend(await asyncio.gather(*(self.join(shell, sessions[index % 3], keep=True)
                                                for index in range(count))))
            await asyncio.sleep(random.Random(wave).uniform(0.05, 0.3))
        self.assertTrue(await until(lambda: all(sink.data.endswith(b'DONE\r\n') for sink in sinks), 60))
        for sink in sinks:
            self.assertEqual(bytes(sink.data), expected)

    async def test_restart_keeps_every_client_subscribed_to_the_new_process(self):
        shell = await self.shell()
        sinks = [await self.join(shell) for _ in range(3)]
        await self.board.restart(shell)
        self.remember()
        self.assertTrue(await until(lambda: all(sink.data.count(b'ready:') >= 2 for sink in sinks), 10))
        for sink in sinks:
            self.assertGreaterEqual(len(sink.of('state')), 1)
            self.assertIsNone(sink.close_code)
        await asyncio.sleep(0.3)
        self.assertEqual(len({bytes(sink.data) for sink in sinks}), 1, 'all clients saw the same bytes')


# =========================================================================================================
# 2. input
# =========================================================================================================

HEADER = re.compile(rb'\x02C(\d)S(\d{4})L(\d{5}):')


def frame(client: int, seq: int, payload: bytes) -> bytes:
    return b'\x02C%dS%04dL%05d:' % (client, seq, len(payload)) + payload


def parse_frames(record: bytes) -> list[tuple[int, int, bytes]]:
    frames, offset = [], 0
    while offset < len(record):
        match = HEADER.match(record, offset)
        if match is None:
            raise AssertionError(f'a frame header expected at byte {offset}: {record[offset:offset + 40]!r}')
        client, seq, length = (int(group) for group in match.groups())
        start = match.end()
        frames.append((client, seq, record[start:start + length]))
        offset = start + length
    return frames


class InputTests(Env):
    async def concurrent_senders(self, clients: int, per_client: int, seed: int) -> tuple[str, dict[int, list[bytes]]]:
        shell = await self.shell(quiet=True)
        sessions = [self.session, self.login()]
        sinks = [await self.join(shell, sessions[index // 8], keep=False) for index in range(clients)]
        sent: dict[int, list[bytes]] = {index: [] for index in range(clients)}

        async def sender(index: int) -> None:
            rng = random.Random(seed * 100 + index)
            for seq in range(per_client):
                size = 60_000 if rng.random() < 0.03 else rng.randint(1, 3000)
                payload = rng.randbytes(size)
                sent[index].append(payload)
                await sinks[index].type(frame(index, seq, payload))
                if rng.random() < 0.5:
                    await asyncio.sleep(rng.uniform(0, 0.004))
        await asyncio.gather(*(sender(index) for index in range(clients)))
        total = sum(len(frame(i, s, p)) for i, items in sent.items() for s, p in enumerate(items))
        self.assertTrue(await until(lambda: len(self.recorded(shell)) >= total, 60), (len(self.recorded(shell)), total))
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.recorded(shell)), total, 'nothing duplicated or extra')
        return shell, sent

    def check(self, shell: str, sent: dict[int, list[bytes]]) -> None:
        frames = parse_frames(self.recorded(shell))
        per_client: dict[int, list[tuple[int, bytes]]] = {}
        for client, seq, payload in frames:
            per_client.setdefault(client, []).append((seq, payload))
        for client, items in sent.items():
            got = per_client.get(client, [])
            self.assertEqual([seq for seq, _ in got], list(range(len(items))), f'client {client}: order or loss')
            self.assertEqual([payload for _, payload in got], items, f'client {client}: payload')

    async def test_two_clients_typing_at_once_keep_each_ones_frames_whole_and_in_order(self):
        self.check(*await self.concurrent_senders(2, 120, 1))

    async def test_four_clients_typing_at_once_keep_each_ones_frames_whole_and_in_order(self):
        self.check(*await self.concurrent_senders(4, 100, 2))

    async def test_eight_clients_typing_at_once_keep_each_ones_frames_whole_and_in_order(self):
        shell, sent = await self.concurrent_senders(8, 60, 3)
        self.check(shell, sent)
        frames = parse_frames(self.recorded(shell))
        owners = [client for client, _, _ in frames]
        self.assertGreater(len([1 for a, b in zip(owners, owners[1:]) if a != b]), 20, 'the clients really interleaved')

    async def test_korean_text_split_inside_characters_arrives_byte_exact_and_every_client_sees_it(self):
        shell = await self.shell()
        a, b, c = [await self.join(shell) for _ in range(3)]
        text = '안녕하세요 한글 입력, 🙂 이모지와 혼합\n' * 3
        data = text.encode()
        rng = random.Random(7)
        cuts, offset = [], 0
        while offset < len(data):
            step = rng.randint(1, 5)
            cuts.append(data[offset:offset + step])
            offset += step
        broken = 0
        for piece in cuts:
            try:
                piece.decode()
            except UnicodeDecodeError:
                broken += 1
        self.assertGreater(broken, 10, 'many pieces cut a character in two')
        for piece in cuts:
            await a.type(piece)
        self.assertTrue(await until(lambda: self.recorded(shell) == data, 10))
        for sink in (a, b, c):
            self.assertTrue(await until(lambda sink=sink: sink.data.count(b'<') >= 1 and
                                        sink.data.replace(b'<', b'').replace(b'>', b'').endswith(data), 10))
        self.assertEqual(bytes(a.data), bytes(b.data))
        self.assertEqual(bytes(b.data), bytes(c.data))
        self.assertEqual(bytes(a.data).replace(b'<', b'').replace(b'>', b'').split(b'\r\n', 1)[1].decode(), text)

    async def test_korean_from_one_client_survives_another_clients_whole_frames_between_the_pieces(self):
        """Frames are atomic: removing the other client's tokens leaves the Korean client's bytes exactly."""
        shell = await self.shell(quiet=True)
        a, b = await self.join(shell), await self.join(shell)
        data = ('가나다라마바사 한글 ' * 40).encode()
        tokens = [b'#B%03d#' % index for index in range(80)]

        async def korean():
            for index in range(0, len(data), 2):  # 2-byte pieces: most cut a 3-byte syllable
                await a.type(data[index:index + 2])
                await asyncio.sleep(0)

        async def other():
            for token in tokens:
                await b.type(token)
                await asyncio.sleep(0.002)
        await asyncio.gather(korean(), other())
        total = len(data) + sum(len(token) for token in tokens)
        self.assertTrue(await until(lambda: len(self.recorded(shell)) >= total, 10))
        record = self.recorded(shell)
        for token in tokens:
            self.assertEqual(record.count(token), 1)
            record = record.replace(token, b'')
        self.assertEqual(record, data)
        self.assertEqual(record.decode(), ('가나다라마바사 한글 ' * 40))

    async def test_one_clients_frame_over_64_kib_is_dropped_and_the_others_input_goes_on(self):
        shell = await self.shell(quiet=True)
        a, b = await self.join(shell), await self.join(shell)
        await a.type(b'A' * (64 * 1024 + 1))
        await b.type(b'B-after')
        self.assertTrue(await a.wait_event('error', 1))
        self.assertEqual(a.of('error')[0], {'t': 'error', 'reason': 'too_large'})
        self.assertTrue(await until(lambda: self.recorded(shell) == b'B-after', 5))
        await a.type(b'A' * (64 * 1024))  # exactly the limit is accepted
        self.assertTrue(await until(lambda: len(self.recorded(shell)) == 7 + 64 * 1024, 5))
        self.assertFalse(b.of('error'))


# =========================================================================================================
# 3. the 9th client
# =========================================================================================================

class NinthClientTests(Env):
    async def test_the_ninth_client_is_refused_4429_without_touching_the_eight_at_work(self):
        shell = await self.shell(quiet=True, go='60000,100,2000;')
        sessions = [self.session, self.login()]
        sinks = [await self.join(shell, sessions[index // 4], size=(24 + index, 80), keep=True) for index in range(8)]
        self.assertEqual([sink.attached['clients'] for sink in sinks], list(range(1, 9)))
        size_before = self.board.size(shell)
        intruders = []
        for index in range(5):  # the 9th ... 13th, each wanting a different, absurd window
            tab = await self.raw(shell, sessions[1])
            await tab.hello(sessions[1].csrf, size=(99 + index, 199))
            intruders.append(tab)
        for tab in intruders:
            self.assertEqual(await tab.event('refused'), {'t': 'refused', 'reason': 'too_many_clients', 'limit': 8})
            self.assertEqual(await tab.wait_closed(5), 4429)
            self.assertFalse(tab.output, 'a refused client receives no terminal bytes')
        await sinks[3].type(b'still-here')  # the work goes on
        expected = full_stream(self.labels[shell], 60_000)
        self.assertTrue(await until(lambda: all(sink.data.endswith(b'DONE\r\n') for sink in sinks), 60))
        for sink in sinks:
            self.assertIsNone(sink.close_code)
            self.assertEqual(bytes(sink.data), expected)
            self.assertEqual(sink.of('clients'), [{'t': 'clients', 'count': n} for n in range(sink.attached['clients'] + 1, 9)])
        self.assertEqual(self.terminal.clients(shell), 8)
        self.assertEqual(self.board.view(shell)['clients'], 8)
        self.assertEqual(self.board.size(shell), (24 + 3, 80), 'the last typist (index 3) owns the size')
        self.assertNotEqual(self.board.size(shell), (99, 199))
        self.assertEqual(size_before, (24, 80))
        self.assertEqual(self.recorded(shell), b'still-here')
        await self.audited('terminal_denied', 5)
        await self.audited('terminal_attach', 8)
        rows = self.audit()
        denied = [detail for event, detail in rows if event == 'terminal_denied']
        self.assertEqual([d['reason'] for d in denied], ['too_many_clients'] * 5)
        self.assertEqual([d['clients'] for d in denied], [8] * 5)
        self.assertEqual(len([1 for event, _ in rows if event == 'terminal_attach']), 8)

    async def test_a_released_place_is_taken_by_exactly_one_of_two_racing_newcomers(self):
        shell = await self.shell(quiet=True)
        sessions = [self.session, self.login()]
        sinks = [await self.join(shell, sessions[index // 4], keep=False) for index in range(8)]
        new = [await self.raw(shell, sessions[1]) for _ in range(2)]
        await sinks[0].send({'t': 'release'})
        self.assertEqual(await sinks[0].wait_closed(5), 1000)
        await asyncio.gather(*(tab.hello(sessions[1].csrf) for tab in new))
        results = []
        for tab in new:
            item = await tab.receive_until(lambda i: isinstance(i, dict) and i['t'] in ('attached', 'refused'))
            results.append(item['t'])
        self.assertEqual(sorted(results), ['attached', 'refused'])
        self.assertEqual(self.terminal.clients(shell), 8)


# =========================================================================================================
# 4. size ownership
# =========================================================================================================

class SizeTests(Env):
    async def test_alternating_typists_cost_one_size_message_per_switch_and_then_silence(self):
        shell = await self.shell(quiet=True)
        a, b = await self.join(shell, size=(24, 80)), await self.join(shell, size=(40, 120))
        rounds = 40
        for index in range(rounds):
            await (b if index % 2 == 0 else a).type(b'x')
            self.assertTrue(await a.wait_event('size', index + 1, 5), f'round {index}')
        await asyncio.sleep(1.5)  # a loop, if any, would keep counting
        for sink in (a, b):
            sizes = sink.of('size')
            self.assertEqual(len(sizes), rounds)
            self.assertEqual([(s['rows'], s['cols']) for s in sizes],
                             [(40, 120), (24, 80)] * (rounds // 2))
        self.assertEqual([e for e in a.of('size')], [e for e in b.of('size')])

    async def test_each_clients_bytes_reach_the_program_under_that_clients_window(self):
        shell = await self.shell()
        windows = [(24, 80), (40, 120), (10, 30), (50, 200)]
        sinks = [await self.join(shell, size=window) for window in windows]
        order = random.Random(11)
        for _ in range(30):
            index = order.randrange(4)
            self.assertEqual(await self.size_of(sinks[index]), 'size:%d,%d;' % windows[index])
        await asyncio.sleep(0.3)
        for sink in sinks:  # nobody is ever told a size that does not belong to somebody's window
            self.assertTrue({(e['rows'], e['cols']) for e in sink.of('size')} <= set(windows))

    async def test_resize_wins_over_the_earlier_typist_and_a_non_owner_resize_takes_ownership(self):
        shell = await self.shell(quiet=True)
        a, b = await self.join(shell, size=(24, 80)), await self.join(shell, size=(30, 90))
        await b.send({'t': 'resize', 'rows': 33, 'cols': 99})
        await until(lambda: self.board.size(shell) == (33, 99))
        await a.type(b'a')  # a is not the owner: its remembered 24x80 returns
        self.assertTrue(await until(lambda: self.board.size(shell) == (24, 80)))
        await b.type(b'b')  # b's last told size is 33x99 (the resize), not the hello's 30x90
        self.assertTrue(await until(lambda: self.board.size(shell) == (33, 99)))
        for sink in (a, b):
            self.assertTrue(await until(lambda sink=sink: len(sink.of('size')) >= 3))
            self.assertEqual([(e['rows'], e['cols']) for e in sink.of('size')], [(33, 99), (24, 80), (33, 99)])

    async def test_hello_size_counts_only_for_the_sole_client_and_never_for_a_refused_or_failed_hello(self):
        shell = await self.shell(quiet=True)
        a = await self.join(shell, size=(30, 100))
        b = await self.join(shell, size=(50, 150))
        c = await self.join(shell, size=(11, 22))
        self.assertEqual(self.board.size(shell), (30, 100))
        for sink in (a, b, c):
            self.assertEqual(sink.of('size'), [])
        await a.send({'t': 'release'})
        await a.wait_closed(5)
        await b.send({'t': 'release'})
        await b.wait_closed(5)
        self.assertEqual(self.board.size(shell), (30, 100), 'the owner leaving keeps the size')
        d = await self.join(shell, size=(60, 200))  # c is still there: d is not alone
        self.assertEqual(d.attached['size'], [30, 100])
        for sink in (c, d):
            await sink.send({'t': 'release'})
            await sink.wait_closed(5)
        self.assertTrue(await until(lambda: self.terminal.clients(shell) == 0))
        e = await self.join(shell, size=(61, 201))  # alone again
        self.assertEqual(e.attached['size'], [61, 201])
        wrong = await self.raw(shell)  # a failed hello (bad CSRF) with a size
        await wrong.hello('not-the-csrf', size=(7, 7))
        self.assertEqual(await wrong.wait_closed(5), 4403)
        self.assertEqual(self.board.size(shell), (61, 201))

    async def test_sizes_are_clamped_in_hello_and_refused_in_resize(self):
        shell = await self.shell(quiet=True)
        sink = await self.join(shell, size=(0, 9999))
        self.assertEqual(sink.attached['size'], [1, 500])
        for bad in ({'rows': 0, 'cols': 80}, {'rows': 24, 'cols': 501}, {'rows': 24.0, 'cols': 80},
                    {'rows': True, 'cols': 80}, {'rows': '24', 'cols': 80}, {'rows': -1, 'cols': 80}, {'cols': 80}):
            await sink.send({'t': 'resize', **bad})
        self.assertTrue(await sink.wait_event('error', 7, 5))
        self.assertEqual({e['reason'] for e in sink.of('error')}, {'bad_resize'})
        self.assertEqual(self.board.size(shell), (1, 500))
        await sink.send({'t': 'resize', 'rows': 500, 'cols': 1})
        self.assertTrue(await until(lambda: self.board.size(shell) == (500, 1)))
        self.assertIsNone(sink.close_code)

    async def test_a_client_that_never_told_its_size_does_not_resize_the_program_when_it_types(self):
        """SPEC: the server remembers the size each connection last told (hello ``size``, ``resize``). A client
        whose hello carried none has told none, so its typing must leave the program's window alone."""
        shell = await self.shell()
        a = await self.join(shell, size=(24, 80))
        tab = await self.raw(shell)
        await tab.hello(self.session.csrf, size=None)
        attached = await tab.event('attached')
        self.assertEqual(attached['size'], [24, 80])
        sink = Sink(tab, attached)
        self.sinks.append(sink)
        await a.send({'t': 'resize', 'rows': 50, 'cols': 200})
        self.assertTrue(await until(lambda: self.board.size(shell) == (50, 200)))
        await sink.type(b'x')
        await asyncio.sleep(0.4)
        self.assertEqual(self.board.size(shell), (50, 200))


# =========================================================================================================
# 5. one slow client, paste backpressure
# =========================================================================================================

class SlowClientTests(Env):
    LINES = 2_400_000  # 21.6 MB: more than the 4 MiB queue plus both kernel buffers can hold

    async def test_a_client_that_stops_reading_gets_4003_alone_while_three_fast_clients_get_every_byte(self):
        shell = await self.shell(quiet=True)
        label = self.labels[shell]
        slow = Tab(self.url, self.session.token, shell)
        self.tabs.append(slow)
        slow_attached = await slow.attach(self.session.csrf)  # then it never reads again
        fast = [await self.join(shell, keep=False) for _ in range(3)]
        typist = fast[0]
        start = b'GO%d,400,0;' % self.LINES
        await typist.type(start)
        expected = full_digest(label, self.LINES)
        typed = []
        started = time.monotonic()
        # keep typing and pinging while the flood runs: the PTY and the Hub loop must stay live
        while any(sink.length < expected[1] for sink in fast) and time.monotonic() - started < 120:
            piece = b'k%03d' % len(typed)
            typed.append(piece)
            await typist.type(piece)
            await typist.send({'t': 'ping', 'n': len(typed)})
            await asyncio.sleep(0.05)
        self.assertEqual([sink.digest for sink in fast], [expected] * 3)
        self.assertTrue(await until(lambda: self.terminal.clients(shell) == 3, 20), self.terminal.clients(shell))
        self.assertEqual(slow_attached['t'], 'attached')
        self.assertEqual(self.board.view(shell)['state'], 'running')
        self.assertEqual(len(self.board.find(shell).subscribers), 3)
        self.assertTrue(await until(lambda: self.recorded(shell) == start + b''.join(typed), 10), len(typed))
        self.assertTrue(await until(lambda: len(typist.of('pong')) == len(typed), 5), (len(typist.of('pong')), len(typed)))
        for sink in fast:
            self.assertIsNone(sink.close_code)
            self.assertEqual(sink.of('clients')[-1], {'t': 'clients', 'count': 3})
        closed = await self.audited('terminal_closed', 1)
        self.assertEqual([(d['client'], d['clients']) for d in closed], [(1, 3)])
        async with aiohttp.ClientSession() as http:  # the Hub loop is not stuck
            started = time.monotonic()
            async with http.get(self.url + '/login') as response:
                self.assertEqual(response.status, 200)
            self.assertLess(time.monotonic() - started, 2)
        self.assertIn(await asyncio.wait_for(self.drain_until_closed(slow), 60), (4003, 1006))

    async def drain_until_closed(self, tab: Tab) -> int | None:
        while True:
            message = await tab.ws.receive()
            if message.type not in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                return tab.ws.close_code if tab.ws.close_code is not None else 1006


class PasteBackpressureTests(Env):
    HEARTBEAT = 0.5
    SLACK = 0.25  # 1.25 s of silence ends a connection; a paced paste is not silence

    async def paste(self, sink: Sink, index: int, frames: int, errors: list) -> None:
        rng = random.Random(index)
        for seq in range(frames):
            await sink.type(frame(index, seq, rng.randbytes(65_000 - len(frame(index, seq, b'')))))

    async def test_a_paced_paster_neither_stalls_the_others_nor_times_out_and_loses_nothing(self):
        shell = await self.shell(hold=True, quiet=True, go='400000,10,3000;')
        paster, watcher = await self.join(shell, keep=False), await self.join(shell, keep=False)
        pace_task = asyncio.create_task(self.paste(paster, 0, 50, []))  # 3.2 MB of paste
        await asyncio.sleep(0.3)
        marks = []
        end = time.monotonic() + 4.0  # 3x the silence limit with the paster stuck
        while time.monotonic() < end:
            sent = time.monotonic()
            await watcher.send({'t': 'ping', 'n': len(marks)})
            self.assertTrue(await until(lambda: len(watcher.of('pong')) > len(marks), 1.5), 'the watcher is answered')
            marks.append(time.monotonic() - sent)
            await asyncio.sleep(0.2)
        self.assertLess(max(marks), 0.5)
        self.assertLess(watcher.max_gap, 1.0, 'the watcher keeps receiving output while the paster is paced')
        self.assertIsNone(paster.close_code, 'a paced paster is not silent')
        self.assertFalse(paster.of('error'))
        backlog = self.board.input_backlog(shell)  # the Hub stopped reading the paster at the high water mark
        self.assertLessEqual(backlog, (1 << 20) + 65536)
        self.assertGreater(backlog, 900 << 10)
        self.release_hold(shell)
        await asyncio.wait_for(pace_task, 30)
        total = 50 * 65_000
        self.assertTrue(await until(lambda: len(self.recorded(shell)) >= total, 30), len(self.recorded(shell)))
        await asyncio.sleep(0.3)
        frames = parse_frames(self.recorded(shell))
        self.assertEqual([seq for _, seq, _ in frames], list(range(50)))
        rng = random.Random(0)
        for seq, (_, _, payload) in enumerate(frames):
            self.assertEqual(payload, rng.randbytes(65_000 - len(frame(0, seq, b''))))
        self.assertIsNone(paster.close_code)
        self.assertIsNone(watcher.close_code)

    async def test_eight_clients_pasting_at_once_into_a_stuck_program_lose_and_refuse_nothing(self):
        shell = await self.shell(hold=True, quiet=True)
        sessions = [self.session, self.login()]
        sinks = [await self.join(shell, sessions[index // 4], keep=False) for index in range(8)]
        errors: list = []
        tasks = [asyncio.create_task(self.paste(sink, index, 24, errors)) for index, sink in enumerate(sinks)]
        await asyncio.sleep(2.0)  # past the silence limit: all of them paced, none timed out
        self.assertLess(self.board.input_backlog(shell), 2 << 20, 'about the high water mark, far below the hard cap')
        self.assertTrue(all(sink.close_code is None for sink in sinks))
        self.release_hold(shell)
        await asyncio.wait_for(asyncio.gather(*tasks), 60)
        total = 8 * 24 * 65_000
        self.assertTrue(await until(lambda: len(self.recorded(shell)) >= total, 60), self.diag(shell))
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.recorded(shell)), total)
        per_client: dict[int, list[int]] = {}
        for client, seq, payload in parse_frames(self.recorded(shell)):
            per_client.setdefault(client, []).append(seq)
            self.assertEqual(len(payload), 65_000 - len(frame(client, seq, b'')))
        self.assertEqual(per_client, {index: list(range(24)) for index in range(8)})
        for sink in sinks:
            self.assertFalse(sink.of('error'), sink.of('error'))


# =========================================================================================================
# 6. heartbeat, terminal gone, Hub stop
# =========================================================================================================

class HeartbeatTests(Env):
    HEARTBEAT = 0.4
    SLACK = 0.2  # 1.0 s

    async def test_the_silent_client_alone_gets_4002_and_the_pinging_ones_never_notice(self):
        shell = await self.shell(quiet=True, go='100000,50,2000;')
        silent = await self.join(shell, keep=False)
        pingers = [await self.join(shell, keep=False) for _ in range(2)]
        started = time.monotonic()
        count = 0
        while silent.close_code is None and time.monotonic() - started < 6:
            count += 1
            for sink in pingers:
                await sink.send({'t': 'ping', 'n': count})
            await asyncio.sleep(0.15)
        elapsed = time.monotonic() - started
        self.assertEqual(silent.close_code, 4002)
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLessEqual(elapsed, 2.5)
        for sink in pingers:
            self.assertIsNone(sink.close_code)
            self.assertEqual(sink.of('clients')[-1], {'t': 'clients', 'count': 2})
        self.assertTrue(await until(lambda: [e for e, _ in self.audit()].count('terminal_timeout') == 1))
        timeout = [d for e, d in self.audit() if e == 'terminal_timeout'][0]
        self.assertEqual((timeout['client'], timeout['clients']), (1, 2))
        self.assertEqual(self.terminal.clients(shell), 2)


class GoneAndStopTests(Env):
    async def test_closing_a_shell_with_eight_clients_and_output_flowing_ends_everyone_with_gone_4404(self):
        shell = await self.shell(quiet=True, go='200000,100,1000;')
        sessions = [self.session, self.login()]
        sinks = [await self.join(shell, sessions[index // 4], keep=False) for index in range(8)]
        await asyncio.sleep(0.2)
        await self.board.close(shell)
        for sink in sinks:
            self.assertEqual(await sink.wait_closed(10), 4404)
            self.assertEqual(sink.of('gone'), [{'t': 'gone'}])
        self.assertTrue(await until(lambda: len([1 for e, _ in self.audit() if e == 'terminal_closed']) == 8))
        closed = [d for e, d in self.audit() if e == 'terminal_closed']
        self.assertEqual(sorted(d['clients'] for d in closed), list(range(8)))
        self.assertEqual(self.terminal.clients(shell), 0)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.raw(shell)
        self.assertEqual(caught.exception.status, 404)

    async def test_deleting_an_agent_ends_all_its_clients_with_gone_4404(self):
        agent_id = await self.agent(quiet=True)
        sinks = [await self.join(agent_id, keep=False) for _ in range(3)]
        await self.board.delete(agent_id)
        for sink in sinks:
            self.assertEqual(await sink.wait_closed(10), 4404)
            self.assertEqual(sink.of('gone'), [{'t': 'gone'}])

    async def test_hub_stop_closes_all_eight_even_with_one_that_never_reads(self):
        shell = await self.shell(quiet=True, go='900000,400,0;')
        reader_sinks = [await self.join(shell, keep=False) for _ in range(6)]
        stuck = Tab(self.url, self.session.token, shell)
        self.tabs.append(stuck)
        await stuck.attach(self.session.csrf)
        quiet_sink = await self.join(shell, keep=False)
        await asyncio.sleep(0.5)
        stoppers = list(self.app[WEBSOCKETS])
        self.assertEqual(len(stoppers), 8)
        started = time.monotonic()
        await asyncio.gather(*(s.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'hub stopping') for s in stoppers))
        for sink in reader_sinks + [quiet_sink]:
            self.assertEqual(await sink.wait_closed(10), 1001)
        self.assertLess(time.monotonic() - started, board_ws.CLOSE_DEADLINE + board_ws.CLOSE_WAIT + 2)
        self.assertTrue(await until(lambda: not self.terminal.connections, 10))
        await self.terminal.flush()
        self.assertEqual([e for e, _ in self.audit()].count('terminal_closed'), 8)


# =========================================================================================================
# 7. caps, upgrade refusals, message validation
# =========================================================================================================

class RealCapTests(Env):
    HELLO = 60.0

    async def upgrade(self, http: aiohttp.ClientSession, shell: str, session) -> tuple[int, str, object]:
        try:
            ws = await http.ws_connect(f'{self.url}/ws/board/{shell}', headers={
                'Cookie': f'wb_hub_session={session.token}', 'Origin': self.url})
            return 101, '', ws
        except aiohttp.WSServerHandshakeError as error:
            return error.status, '', None

    async def test_the_shipped_128_total_and_8_pending_caps_with_real_sockets(self):
        shell = await self.shell(quiet=True)
        self.assertEqual((self.terminal.max_connections, self.terminal.max_pending), (128, 8))
        sessions = [self.login() for _ in range(18)]
        opened = []
        connector = aiohttp.TCPConnector(limit=0)  # (the default of 100 connections would stall the 101st)
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), connector=connector) as http:
            try:
                for _ in range(8):
                    status, _, ws = await self.upgrade(http, shell, sessions[0])
                    self.assertEqual(status, 101)
                    opened.append(ws)
                status, _, _ = await self.upgrade(http, shell, sessions[0])
                self.assertEqual(status, 429, 'the 9th socket of a session before any hello')
                for session in sessions[1:16]:  # 15 x 8 more: 128 in all
                    for _ in range(8):
                        status, _, ws = await self.upgrade(http, shell, session)
                        self.assertEqual(status, 101)
                        opened.append(ws)
                self.assertEqual(len(opened), 128)
                self.assertEqual(self.terminal.open, 128)
                status, _, _ = await self.upgrade(http, shell, sessions[16])
                self.assertEqual(status, 503, 'the 129th socket')
                status, _, _ = await self.upgrade(http, shell, sessions[0])
                self.assertEqual(status, 503, 'the total cap is checked before the per-session one')
                # an attached socket still counts toward the total, and frees its pending place
                await opened[0].send_str(json.dumps({'t': 'hello', 'v': 2, 'csrf': sessions[0].csrf, 'size': [24, 80]}))
                message = await asyncio.wait_for(opened[0].receive(), 5)
                self.assertEqual(json.loads(message.data)['t'], 'attached')
                status, _, _ = await self.upgrade(http, shell, sessions[16])
                self.assertEqual(status, 503)
                await opened[1].close()  # one leaves: one place
                self.assertTrue(await until(lambda: self.terminal.open == 127, 5))
                status, _, ws = await self.upgrade(http, shell, sessions[0])
                self.assertEqual(status, 101, 'session 0 has 7 pending after its hello and a close')
                opened.append(ws)
                status, _, _ = await self.upgrade(http, shell, sessions[16])
                self.assertEqual(status, 503)
            finally:
                for ws in opened:
                    if ws is not None and not ws.closed:
                        await ws.close()
        self.assertTrue(await until(lambda: self.terminal.open == 0, 15), self.terminal.open)
        self.assertEqual(self.terminal.pending, {})


class UpgradeTests(Env):
    async def status(self, path: str, headers: dict) -> int:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
            try:
                ws = await http.ws_connect(self.url + path, headers=headers)
            except aiohttp.WSServerHandshakeError as error:
                return error.status
            await ws.close()
            return 101

    async def test_upgrade_refusals_before_any_socket_and_the_csrf_rule_after(self):
        shell = await self.shell(quiet=True)
        cookie = {'Cookie': f'wb_hub_session={self.session.token}'}
        good = {**cookie, 'Origin': self.url}
        path = f'/ws/board/{shell}'
        self.assertEqual(await self.status(path, {'Origin': self.url}), 401)
        self.assertEqual(await self.status(path, {'Origin': self.url, 'Authorization': 'Bearer wbk_not_a_real_key'}), 401)
        self.assertEqual(await self.status(path, cookie), 403)
        self.assertEqual(await self.status(path, {**cookie, 'Origin': 'http://evil.example'}), 403)
        self.assertEqual(await self.status(path, {**cookie, 'Origin': self.url.replace('127.0.0.1', 'localhost')}), 403)
        self.assertEqual(await self.status(path, {'Cookie': 'wb_hub_session=forged', 'Origin': self.url}), 401)
        self.assertEqual(await self.status('/ws/board/s-nothere', good), 404)
        self.assertEqual(await self.status('/ws/board', good), 404)
        self.assertEqual(self.terminal.open, 0)
        for send in (lambda tab: tab.ws.send_str(json.dumps({'t': 'hello', 'v': 2, 'size': [24, 80]})),
                     lambda tab: tab.hello(self.login().csrf)):  # no csrf at all; another session's csrf
            tab = await self.raw(shell)
            await send(tab)
            self.assertEqual(await tab.wait_closed(5), 4403)
        self.assertEqual(self.terminal.clients(shell), 0)
        await self.audited('terminal_denied', 2)
        self.assertEqual([d['reason'] for e, d in self.audit() if e == 'terminal_denied'], ['csrf', 'csrf'])
        self.assertEqual([e for e, _ in self.audit() if e == 'terminal_attach'], [])


class MessageTests(Env):
    async def test_bad_messages_get_an_error_and_never_hurt_the_connection_or_its_neighbours(self):
        shell = await self.shell(quiet=True)
        a, b = await self.join(shell), await self.join(shell)
        await a.type(b'before-')
        for text in ('{"t":"hello","v":2,"csrf":"x"}',  # a second hello
                     '{"t":"zzz"}', '{"t":"resizee","rows":1,"cols":1}', '[1,2]', '"hello"', 'not json', '{}',
                     '{"t":5}', '{"t":null}', '{"t":"hello"}'):
            await a.tab.ws.send_str(text)
        self.assertTrue(await a.wait_event('error', 10, 5), a.of('error'))
        self.assertEqual([e for e in a.of('error')], [{'t': 'error', 'reason': 'bad_message'}] * 10)
        self.assertEqual(self.terminal.clients(shell), 2, 'a second hello never attaches a duplicate')
        self.assertFalse(b.of('error'))
        await a.type(b'after')
        self.assertTrue(await until(lambda: self.recorded(shell) == b'before-after', 5))
        await a.tab.ws.send_str('x' * (64 * 1024 + 1))  # an oversized control message
        self.assertTrue(await a.wait_event('error', 11, 5))
        self.assertEqual(a.of('error')[-1], {'t': 'error', 'reason': 'too_large'})
        self.assertIsNone(a.close_code)

    async def test_a_frame_over_one_mib_closes_that_connection_with_1009_and_only_that_one(self):
        shell = await self.shell(quiet=True)
        a, b = await self.join(shell), await self.join(shell)
        await a.tab.ws.send_bytes(b'z' * ((1 << 20) + 1))
        self.assertEqual(await a.wait_closed(10), 1009)
        self.assertTrue(await until(lambda: self.terminal.clients(shell) == 1))
        self.assertTrue(await b.wait_event('clients', 1, 5))
        self.assertEqual(b.of('clients')[-1], {'t': 'clients', 'count': 1})
        await b.type(b'fine')
        self.assertTrue(await until(lambda: self.recorded(shell) == b'fine', 5))
        self.assertIsNone(b.close_code)
        await self.audited('terminal_closed', 1)
        self.assertEqual([e for e, _ in self.audit()][-1], 'terminal_closed')

    async def test_a_hello_with_the_wrong_version_or_type_is_a_bad_hello_4400(self):
        shell = await self.shell(quiet=True)
        for hello in ({'t': 'hello', 'v': 1, 'csrf': self.session.csrf}, {'t': 'hello', 'v': 2.0, 'csrf': self.session.csrf},
                      {'t': 'hello', 'v': True, 'csrf': self.session.csrf}, {'t': 'hola', 'v': 2},
                      {'t': 'hello', 'v': 2, 'csrf': self.session.csrf, 'size': [24]},
                      {'t': 'hello', 'v': 2, 'csrf': self.session.csrf, 'size': ['a', 'b']}):
            tab = await self.raw(shell)
            await tab.ws.send_str(json.dumps(hello))
            self.assertEqual(await tab.wait_closed(5), 4400, hello)
        self.assertEqual(self.terminal.clients(shell), 0)


# =========================================================================================================
# 8. audit
# =========================================================================================================

class AuditTests(Env):
    HEARTBEAT = 0.4
    SLACK = 0.2

    async def test_connect_and_end_rows_carry_the_client_number_and_the_count_after_and_no_content(self):
        shell = await self.shell()
        secret = b'TOP-SECRET-KEYSTROKES-12345'
        a = await self.join(shell)
        b = await self.join(shell)
        c = await self.join(shell)
        await a.type(secret)
        await a.wait_bytes(b'<' + secret + b'>')
        await b.send({'t': 'release'})
        self.assertEqual(await b.wait_closed(5), 1000)
        await asyncio.sleep(0.2)
        a.tab.kill()  # a browser killed: no close frame
        self.assertTrue(await until(lambda: self.terminal.clients(shell) == 1, 5))
        for _ in range(8):  # c keeps pinging so nothing else times out ...
            await c.send({'t': 'ping', 'n': 1})
            await asyncio.sleep(0.1)
        d = await self.join(shell)  # ... d joins and then goes silent
        for _ in range(14):
            await c.send({'t': 'ping', 'n': 2})
            await asyncio.sleep(0.1)
        self.assertEqual(await d.wait_closed(5), 4002)
        await c.send({'t': 'release'})
        await c.wait_closed(5)
        await self.audited('terminal_release', 2)
        await self.audited('terminal_timeout', 1)
        rows = self.audit()
        self.assertEqual([(e, d_['client'], d_['clients']) for e, d_ in rows],
                         [('terminal_attach', 1, 1), ('terminal_attach', 2, 2), ('terminal_attach', 3, 3),
                          ('terminal_release', 2, 2), ('terminal_closed', 1, 1), ('terminal_attach', 4, 2),
                          ('terminal_timeout', 4, 1), ('terminal_release', 3, 0)])
        for _, detail in rows:
            self.assertEqual(set(detail), {'terminal_id', 'kind', 'name', 'client', 'clients'})
            self.assertEqual((detail['terminal_id'], detail['kind']), (shell, 'shell'))
        dump = (self.state / 'hub.sqlite3').read_bytes() + json.dumps(rows).encode()
        for text in (secret, self.session.csrf.encode(), self.session.token.encode()):
            self.assertNotIn(text, dump)
        db = sqlite3.connect(f'file:{self.state / "hub.sqlite3"}?mode=ro', uri=True)
        try:
            actors = {row[0] for row in db.execute("SELECT actor FROM audit WHERE event LIKE 'terminal_%'")}
        finally:
            db.close()
        self.assertEqual(actors, {'owner'})


if __name__ == '__main__':
    unittest.main()
