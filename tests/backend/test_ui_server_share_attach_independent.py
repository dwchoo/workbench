"""WBX-E1 independent verification of the Hub's shared attach (``share_attach``/``share_detach``) on the backend.

Expected values come from the contract text only: web-board-ux BRIEF WBX-D59..D67 / WBX-AC-39..42, SPEC s5 "트랙 E"
("ui_v1 덧붙이기: 공유 attach"), ticket WBX-E1; not from the implementation's own tests.

Seams (no new seam): part A is the backend ui_server seam, a real ``UiServer`` on a unix socket in an owned temp dir
with a scripted controller (subclass of ``FakeController``) and raw ui_v1 sockets owned by the test (the peer identity
and the verified-Hub check are injected the way Hub SPEC S5 does); part B is a real ``Backend`` (fake OMPs, real host
shell PTY) of the command-stop fixture with real ``UiClient`` connections on the backend's ui socket.

Oracles: bytes/frames the clients receive, the controller's own event log (order of ``on_attach``/``resize``),
the snapshot, the backend journal, and ``stty``-free pane sizes in the snapshot.  This test process plays the Hub.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_ui_server import FakeController
from workbench.backend.client import UiClient
from workbench.backend.ui_server import MAX_CONNECTIONS, MAX_SHARED_ATTACHMENTS, Held, UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Reason
from workbench.contracts.v1 import PaneId

HUB = "workbench-hub"
UI = "workbench-ui"
KiB = 1024
MiB = 1024 * KiB
CID = "7b1c2a9e-3b4d-4e6f-8a7b-0c1d2e3f4a5b"
PANES = ("manager_omp", "worker_omp", "host_shell")


def payload_for(seq: int, size: int) -> bytes:
    unit = f"<{seq:06d}>".encode()
    return (unit * (size // len(unit) + 1))[:size]


def wait_for(predicate, timeout=10.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


# =====================================================================================================================
# Part A: the ui_server seam
# =====================================================================================================================
class PeerServer(UiServer):
    """The peer identity read at accept is injectable: another uid, another pid, or unreadable."""

    uid_offset = 0
    pid_override: int | None = None
    unreadable = False

    def peer_identity(self, sock):
        if self.unreadable:
            raise OSError("no peer credentials")
        pid, uid = super().peer_identity(sock)
        return (self.pid_override if self.pid_override is not None else pid), uid + self.uid_offset


class ScriptedController(FakeController):
    """FakeController plus: a replay ring that follows ``chunk``, an ordered event log, scripted refusals
    (``REFUSE:<reason>`` input) and a gated ``stop_command``."""

    RING = 128 * KiB

    def __init__(self):
        super().__init__()
        self.history: list = []
        self.events: list[tuple] = []
        self.state_n = 0
        self.stop_gate = threading.Event()
        self.stop_gate.set()

    def snapshot(self):
        return {**super().snapshot(), "n": self.state_n}

    def chunk(self, data):
        chunk = super().chunk(data)
        self.history.append(chunk)
        total, kept = 0, []
        for item in reversed(self.history):
            total += len(item.data)
            if total > self.RING and kept:
                break
            kept.append(item)
        self.history = list(reversed(kept))
        return chunk

    def replay(self):
        return list(self.history)

    def on_attach(self, size):
        super().on_attach(size)
        self.events.append(("attach", size))

    def on_detach(self):
        super().on_detach()
        self.events.append(("detach",))

    def resize(self, pane, rows, cols):
        super().resize(pane, rows, cols)
        self.events.append(("resize", None if pane is None else pane.value, rows, cols))

    def set_focus(self, pane):
        super().set_focus(pane)
        self.events.append(("focus", pane.value))

    def admit(self, pane, data, kind):
        if data.startswith(b"REFUSE:"):
            name = data[len(b"REFUSE:"):].decode()
            return Reason(name), f"scripted {name}"
        return super().admit(pane, data, kind)

    def stop_command(self, command_id):
        self.stop_gate.wait(10)
        return {"status": "exited", "command_id": command_id, "requested_by": "user"}


class Raw:
    """A raw ui_v1 client that records every frame in order; a reader thread unless ``read=False`` (a client that
    does not read, with a tiny receive buffer)."""

    _ids = 0

    def __init__(self, path, name, *, read=True, rcvbuf=None, hello=True):
        self.sock = socket.socket(socket.AF_UNIX)
        if rcvbuf:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.settimeout(10)
        self.sock.connect(str(path))
        self.decoder = ui_v1.FrameDecoder()
        self.frames: list[ui_v1.Frame] = []
        self.lock = threading.Lock()
        self.eof = threading.Event()
        self.thread = None
        if hello:
            self.sock.sendall(ui_v1.encode_frame(ui_v1.hello(name)))
        if read:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            assert wait_for(lambda: any(f.header.get("type") == "welcome" for f in self.snapshot_frames()), 10), \
                f"no welcome for {name!r}"

    def _run(self):
        while True:
            try:
                data = self.sock.recv(1 << 16)
            except (OSError, socket.timeout):
                break
            if not data:
                break
            try:
                frames = self.decoder.feed(data)
            except Exception as exc:  # a stream the contract cannot decode is itself a finding
                frames = [ui_v1.Frame({"type": "_undecodable", "error": repr(exc)}, b"")]
            with self.lock:
                self.frames.extend(frames)
        self.eof.set()

    def snapshot_frames(self):
        with self.lock:
            return list(self.frames)

    def drain_until_eof(self, timeout=15.0):
        """For a client that did not read: read everything the server wrote until it closed."""
        deadline = time.monotonic() + timeout
        self.sock.settimeout(1.0)
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(1 << 16)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                self.eof.set()
                break
            self.frames.extend(self.decoder.feed(data))
        return self.frames

    def send(self, header, payload=b""):
        self.sock.sendall(ui_v1.encode_frame(header, payload))

    def request(self, kind, payload=b"", *, timeout=10.0, **fields):
        Raw._ids += 1
        rid = f"r{Raw._ids}"
        self.send({"v": ui_v1.VERSION, "type": kind.value if hasattr(kind, "value") else kind, "id": rid, **fields},
                  payload)
        return self.result(rid, timeout)

    def result(self, rid, timeout=10.0, final=False):
        def find():
            for f in self.snapshot_frames():
                h = f.header
                if h.get("type") == "result" and h.get("id") == rid and bool(h.get("final")) == final:
                    return h
            return None
        if self.thread is None:  # a client that does not read in the background: pull by hand until the result
            deadline = time.monotonic() + timeout
            self.sock.settimeout(0.2)
            while find() is None and time.monotonic() < deadline:
                try:
                    data = self.sock.recv(1 << 16)
                except socket.timeout:
                    continue
                if not data:
                    break
                self.frames.extend(self.decoder.feed(data))
        assert wait_for(lambda: find() is not None, timeout), f"no result for {rid}"
        return find()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        if self.thread is not None:
            self.thread.join(5)

    # views ---------------------------------------------------------------------------------------------------------
    def after(self, rid):
        """Frames after the result with id ``rid``."""
        frames = self.snapshot_frames()
        for index, f in enumerate(frames):
            if f.header.get("type") == "result" and f.header.get("id") == rid:
                return frames[index + 1:]
        raise AssertionError(f"no result {rid}")

    @staticmethod
    def stream(frames, replay=None):
        """display and state frames as comparable tuples, in order."""
        out = []
        for f in frames:
            kind = f.header.get("type")
            if kind not in ("display", "state"):
                continue
            if replay is not None and kind == "display" and bool(f.header.get("replay")) != replay:
                continue
            out.append((kind, json.dumps(f.header, sort_keys=True), f.payload))
        return out


def strip(result):
    return {k: v for k, v in result.items() if k != "id"}


class Fixture(unittest.TestCase):
    max_outbound = 8 * MiB

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="wbx-e1v-")
        self.path = Path(self._dir.name) / "ui.sock"
        self.controller = ScriptedController()
        self.hub_pids = {os.getpid()}
        self.server = PeerServer(self.path, self.controller, max_outbound=self.max_outbound,
                                 hub_peer=lambda pid: pid in self.hub_pids)
        self.calls: queue.SimpleQueue = queue.SimpleQueue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.raws: list[Raw] = []

    def _serve(self):
        while not self.stop.is_set():
            self.server.poll(0.005)
            while True:
                try:
                    call, done, box = self.calls.get_nowait()
                except queue.Empty:
                    break
                try:
                    box.append(call())
                except BaseException as exc:
                    box.append(exc)
                done.set()

    def in_loop(self, call):
        done, box = threading.Event(), []
        self.calls.put((call, done, box))
        self.assertTrue(done.wait(10), "the server loop did not run the call")
        if isinstance(box[0], BaseException):
            raise box[0]
        return box[0]

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.server.close(flush_timeout=0.5)
        for raw in self.raws:
            raw.close()
        self.assertFalse(self.path.exists())
        self._dir.cleanup()

    # clients
    def raw(self, name=HUB, **kwargs):
        client = Raw(self.path, name, **kwargs)
        self.raws.append(client)
        return client

    def tui(self, size=None, **kwargs):
        ui = self.raw(UI, **kwargs)
        fields = {} if size is None else {"size": {"rows": size[0], "cols": size[1]}}
        result = ui.request(ClientType.ATTACH, **fields)
        self.assertTrue(result["ok"], result)
        return ui

    def shared(self, **kwargs):
        hub = self.raw(HUB, **kwargs)
        result = hub.request(ClientType.SHARE_ATTACH)
        self.assertTrue(result["ok"], result)
        return hub

    # server-side views (read on the loop thread)
    def shared_count(self):
        return self.in_loop(lambda: len(self.server.shared))

    def attached_is(self, none: bool):
        return self.in_loop(lambda: self.server.attached is None) is none

    def broadcast(self, data):
        def send():
            chunk = self.controller.chunk(data)
            self.server.broadcast(chunk)
            return chunk
        return self.in_loop(send)

    def push_state(self, n):
        def push():
            self.controller.state_n = n
            self.server.push_state(self.controller.snapshot())
        self.in_loop(push)

    @staticmethod
    def live_payloads(client):
        return [f.payload for f in client.snapshot_frames()
                if f.header.get("type") == "display" and not f.header.get("replay")]

    def wait_live(self, client, data, timeout=5.0):
        return wait_for(lambda: data in self.live_payloads(client), timeout)

    def quiet(self, client, seconds=0.25):
        before = len(client.snapshot_frames())
        time.sleep(seconds)
        return len(client.snapshot_frames()) == before


def dead_pid():
    proc = subprocess.Popen(["/bin/true"])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------------------------------------------------------------
class AdmissionTests(Fixture):
    """Only ``workbench-hub`` + same uid + verified running Hub pid; anything else: ``not_attached``, nothing changes."""

    def assert_refused(self, hub, ui):
        before = (self.controller.attaches[:], self.controller.detaches, self.shared_count())
        result = hub.request(ClientType.SHARE_ATTACH)
        self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(result.get("detail"), "share_attach is a Workbench Hub request")
        self.assertEqual((self.controller.attaches, self.controller.detaches, self.shared_count()), before)
        self.assertFalse(self.attached_is(none=True), "the primary is still attached")
        marker = f"after-refusal-{id(hub)}".encode()
        self.broadcast(marker)
        self.push_state(self.controller.state_n + 1)
        self.assertTrue(self.wait_live(ui, marker), "the primary keeps receiving")
        self.assertTrue(self.quiet(hub), "a refused connection receives nothing (no display, no state, no replay)")
        # and it is not a shared attachment for anything else either
        refused_input = hub.request(ClientType.INPUT, b"x", pane="host_shell")
        self.assertEqual(refused_input.get("reason"), "not_attached", refused_input)
        self.assertEqual(hub.request(ClientType.SHARE_DETACH).get("reason"), "not_attached")

    def test_a_spoofed_hello_name_even_with_the_right_uid_and_pid(self):
        ui = self.tui()
        for name in (UI, "Workbench-Hub", "workbench-hub-evil", "workbench-hu", "workbench-cli", "x"):
            with self.subTest(name=name):
                self.assert_refused(self.raw(name), ui)

    def test_a_name_that_matches_but_a_different_uid(self):
        ui = self.tui()
        self.server.uid_offset = 1
        hub = self.raw(HUB)
        self.server.uid_offset = 0
        self.assert_refused(hub, ui)

    def test_a_name_and_uid_that_match_but_another_pid(self):
        ui = self.tui()
        for pid in (1, os.getppid(), os.getpid() + 100000):
            with self.subTest(pid=pid):
                self.server.pid_override = pid
                hub = self.raw(HUB)
                self.server.pid_override = None
                self.assert_refused(hub, ui)

    def test_a_dead_hub_pid(self):
        ui = self.tui()
        self.server.pid_override = dead_pid()
        hub = self.raw(HUB)
        self.server.pid_override = None
        self.assert_refused(hub, ui)

    def test_the_hub_stopped_being_verified_so_a_new_connection_is_refused(self):
        first = self.shared()
        self.hub_pids.clear()
        late = self.raw(HUB)
        result = late.request(ClientType.SHARE_ATTACH)
        self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(self.shared_count(), 1)
        self.hub_pids.add(os.getpid())
        self.broadcast(b"x")
        self.assertTrue(self.wait_live(first, b"x"))
        self.assertTrue(self.quiet(late))

    def test_an_unreadable_peer(self):
        ui = self.tui()
        self.server.unreadable = True
        hub = self.raw(HUB)
        self.server.unreadable = False
        self.assert_refused(hub, ui)

    def test_the_ui_connection_that_is_the_primary_cannot_share_attach(self):
        ui = self.tui()
        result = ui.request(ClientType.SHARE_ATTACH)
        self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(self.shared_count(), 0)
        self.assertFalse(self.attached_is(none=True))
        self.assertEqual(ui.request(ClientType.SHARE_DETACH).get("reason"), "not_attached")
        self.assertFalse(self.attached_is(none=True), "a share_detach from the primary does not detach it")
        self.assertEqual(self.controller.detaches, 0)


class SlotTests(Fixture):
    def test_a_hub_named_primary_cannot_share_attach_and_stays_primary(self):
        hub = self.raw(HUB)
        self.assertTrue(hub.request(ClientType.ATTACH)["ok"])
        result = hub.request(ClientType.SHARE_ATTACH)
        self.assertEqual((result["ok"], result.get("reason")), (False, "invalid_message"), result)
        self.assertEqual((self.shared_count(), self.controller.attaches), (0, [None]))
        self.assertFalse(self.attached_is(none=True))
        again = hub.request(ClientType.ATTACH)  # an idempotent re-attach by the primary is the current behaviour
        self.assertTrue(again["ok"])
        self.assertEqual(self.controller.attaches, [None])
        self.broadcast(b"live")
        self.assertTrue(self.wait_live(hub, b"live"))
        time.sleep(0.2)
        self.assertEqual(self.live_payloads(hub).count(b"live"), 1, "one copy, not a primary plus a shared copy")

    def test_a_double_share_is_refused_and_does_not_double_the_stream(self):
        hub = self.shared()
        result = hub.request(ClientType.SHARE_ATTACH)
        self.assertEqual((result["ok"], result.get("reason")), (False, "invalid_message"), result)
        self.assertEqual(self.shared_count(), 1)
        self.broadcast(b"once")
        self.push_state(5)
        self.assertTrue(self.wait_live(hub, b"once"))
        time.sleep(0.2)
        self.assertEqual(self.live_payloads(hub).count(b"once"), 1)
        self.assertEqual(sum(1 for f in hub.snapshot_frames() if f.header.get("type") == "state"), 1)

    def test_a_shared_connection_cannot_attach_and_the_slot_stays_free(self):
        hub = self.shared()
        result = hub.request(ClientType.ATTACH, size={"rows": 40, "cols": 100})
        self.assertEqual((result["ok"], result.get("reason")), (False, "invalid_message"), result)
        self.assertTrue(self.attached_is(none=True))
        self.assertEqual(self.controller.attaches, [])
        self.assertEqual(self.shared_count(), 1)
        self.tui()  # the free primary slot still belongs to the TUI

    def test_a_fifth_shared_attachment_is_queue_full_and_the_four_keep_receiving(self):
        self.assertEqual(MAX_SHARED_ATTACHMENTS, 4)
        four = [self.shared() for _ in range(4)]
        fifth = self.raw(HUB)
        result = fifth.request(ClientType.SHARE_ATTACH)
        self.assertEqual((result["ok"], result.get("reason")), (False, "queue_full"), result)
        self.assertEqual(self.shared_count(), 4)
        self.broadcast(b"to-four")
        for hub in four:
            self.assertTrue(self.wait_live(hub, b"to-four"))
        self.assertTrue(self.quiet(fifth), "the refused fifth gets nothing")
        self.assertEqual(fifth.request(ClientType.INPUT, b"x", pane="host_shell").get("reason"), "not_attached")
        self.assertTrue(four[0].request(ClientType.SHARE_DETACH)["detached"])
        self.assertTrue(fifth.request(ClientType.SHARE_ATTACH)["ok"], "a freed place is reusable")
        self.assertEqual(self.shared_count(), 4)

    def test_four_shared_do_not_take_the_primary_place_nor_the_connection_cap(self):
        four = [self.shared() for _ in range(4)]
        ui = self.tui()
        second = self.raw(UI)
        refused = second.request(ClientType.ATTACH)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "attached_elsewhere"), refused)
        self.assertEqual(self.controller.attaches, [None])
        others = [self.raw(UI) for _ in range(MAX_CONNECTIONS - len(four) - 2)]  # ui, second, four shared, rest
        self.assertEqual(len(self.in_loop(lambda: list(self.server.connections))), MAX_CONNECTIONS)
        over = Raw(self.path, UI, hello=False, read=False)
        self.raws.append(over)
        over.send(ui_v1.hello(UI))
        frames = over.drain_until_eof(5)
        self.assertEqual(frames[0].header.get("type"), "reject", "the 17th connection is still refused")
        self.assertEqual(frames[0].header.get("reason"), "protocol_error")
        self.broadcast(b"x")
        for client in (ui, *four):
            self.assertTrue(self.wait_live(client, b"x"))
        for client in others:
            self.assertTrue(self.quiet(client, 0.05))


class UnknownFieldTests(Fixture):
    BAD = ({"size": {"rows": 30, "cols": 100}}, {"pane": "host_shell"}, {"shared": True}, {"rows": 1}, {"x": None},
           {"client": "workbench-ui"})

    def test_share_attach_with_any_unknown_field_is_invalid_message_and_nothing_changes(self):
        ui = self.tui()
        hub = self.raw(HUB)
        for extra in self.BAD:
            with self.subTest(extra=extra):
                result = hub.request(ClientType.SHARE_ATTACH, **extra)
                self.assertEqual((result["ok"], result.get("reason")), (False, "invalid_message"), result)
                self.assertEqual(self.shared_count(), 0)
                self.assertTrue(self.quiet(hub, 0.05))
        payload = hub.request(ClientType.SHARE_ATTACH, b"payload")
        self.assertEqual((payload["ok"], payload.get("reason")), (False, "invalid_message"), payload)
        self.assertEqual(self.shared_count(), 0)
        self.assertEqual(self.controller.attaches, [None])
        self.assertFalse(self.attached_is(none=True))
        self.assertTrue(hub.request(ClientType.SHARE_ATTACH)["ok"], "the valid form still works afterwards")
        self.assertTrue(ui.request(ClientType.SNAPSHOT)["ok"])

    def test_a_request_without_an_id_is_refused(self):
        hub = self.raw(HUB)
        hub.send({"v": 1, "type": "share_attach"})
        self.assertTrue(wait_for(lambda: any(f.header.get("type") == "result" and f.header.get("ok") is False
                                             and f.header.get("reason") == "invalid_message"
                                             for f in hub.snapshot_frames()), 5))
        self.assertEqual(self.shared_count(), 0)

    def test_share_detach_with_a_field_does_not_detach(self):
        hub = self.shared()
        for extra in ({"pane": "host_shell"}, {"all": True}):
            result = hub.request(ClientType.SHARE_DETACH, **extra)
            self.assertEqual((result["ok"], result.get("reason")), (False, "invalid_message"), result)
            self.assertEqual(self.shared_count(), 1)
        self.broadcast(b"still")
        self.assertTrue(self.wait_live(hub, b"still"))

    def test_an_old_style_attach_carrying_a_share_field_is_just_a_primary_attach(self):
        """WBX-D65 (1): a field on ``attach`` cannot smuggle a shared attachment in."""
        client = self.raw(HUB)
        result = client.request(ClientType.ATTACH, share=True, shared=True, mode="shared")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.shared_count(), 0)
        self.assertFalse(self.attached_is(none=True))
        other = self.raw(UI)
        refused = other.request(ClientType.ATTACH, share=True)
        self.assertEqual(refused.get("reason"), "attached_elsewhere", refused)


class FanOutTests(Fixture):
    def broadcaster(self, count, *, state_every=37, seed=3, batch=25):
        rng = random.Random(seed)

        def run():
            for start in range(0, count, batch):
                def step(start=start):
                    for seq in range(start, min(start + batch, count)):
                        size = rng.randint(1, 3000)
                        self.server.broadcast(self.controller.chunk(payload_for(seq, size)))
                        if seq % state_every == 0:
                            self.controller.state_n = seq
                            self.server.push_state(self.controller.snapshot())
                self.in_loop(step)
        thread = threading.Thread(target=run, daemon=True)
        return thread

    def test_primary_and_every_shared_get_identical_bytes_in_order_and_a_late_joiner_has_no_gap_or_duplicate(self):
        self.controller.history.clear()
        ui = self.tui()
        hubs = [self.shared() for _ in range(3)]
        count = 1500
        producer = self.broadcaster(count)
        producer.start()
        self.assertTrue(wait_for(lambda: self.controller.sequence >= 400, 20))
        late = self.raw(HUB)
        joined = late.request(ClientType.SHARE_ATTACH)  # while output keeps flowing
        self.assertTrue(joined["ok"], joined)
        producer.join(60)
        self.assertFalse(producer.is_alive())
        final = payload_for(count, 10)
        self.broadcast(final)
        everyone = [ui, *hubs, late]
        for client in everyone:
            self.assertTrue(self.wait_live(client, final, 30), "the final chunk reached every attachment")
        # (1) identical display+state sequences after the attach results
        reference = Raw.stream(ui.after(self._attach_id(ui)))
        for index, hub in enumerate(hubs):
            got = Raw.stream(hub.after(self._share_id(hub)))
            replay_free = [x for x in got if not (x[0] == "display" and json.loads(x[1]).get("replay"))]
            ref_free = [x for x in reference if not (x[0] == "display" and json.loads(x[1]).get("replay"))]
            self.assertEqual(replay_free, ref_free, f"shared #{index} differs from the primary")
        self.assertGreater(len([x for x in reference if x[0] == "state"]), 20)
        # (2) the late joiner: replay first, then live; per boundary no gap/duplicate
        frames = late.after(self._share_id(late))
        displays = [f for f in frames if f.header.get("type") == "display"]
        flags = [bool(f.header.get("replay")) for f in displays]
        self.assertEqual(flags, sorted(flags, reverse=True), "all replay frames come before the first live frame")
        seqs = [f.header["sequence"] for f in displays]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))), "replay + live is one contiguous run")
        self.assertEqual(seqs[-1], self.controller.sequence)
        primary_by_seq = {f.header["sequence"]: f.payload for f in ui.snapshot_frames()
                          if f.header.get("type") == "display"}
        for f in displays:
            if f.header["sequence"] in primary_by_seq:
                self.assertEqual(f.payload, primary_by_seq[f.header["sequence"]])
        # the joiner's states are exactly the states the primary got after the joiner's attach (a suffix)
        late_states = [x for x in Raw.stream(frames) if x[0] == "state"]
        ref_states = [x for x in reference if x[0] == "state"]
        self.assertEqual(late_states, ref_states[len(ref_states) - len(late_states):])

    @staticmethod
    def _attach_id(client):
        for f in client.snapshot_frames():
            h = f.header
            if h.get("type") == "result" and "snapshot" in h:
                return h["id"]
        raise AssertionError("no attach result")

    _share_id = _attach_id

    def test_a_shared_attachment_alone_gets_display_and_state(self):
        hub = self.shared()
        self.broadcast(b"a")
        self.push_state(7)
        self.assertTrue(self.wait_live(hub, b"a"))
        self.assertTrue(wait_for(lambda: any(f.header.get("type") == "state" for f in hub.snapshot_frames()), 5))


class SlowSharedTests(Fixture):
    max_outbound = 512 * KiB

    def flood(self, total=3 * MiB, size=16 * KiB):
        sent = []

        def run():
            for seq in range(total // size):
                self.in_loop(lambda seq=seq: self.server.broadcast(self.controller.chunk(payload_for(seq, size))))
                if seq % 20 == 0:
                    self.push_state(seq)
                time.sleep(0.001)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(90)
        self.assertFalse(thread.is_alive())
        return total // size

    def test_one_slow_shared_attachment_closes_alone_with_whole_frames(self):
        ui = self.tui()
        fast_a, fast_b = self.shared(), self.shared()
        slow = self.shared(read=False, rcvbuf=4096)
        self.assertEqual(self.shared_count(), 3)
        count = self.flood()
        self.assertEqual(self.shared_count(), 2, "only the slow connection left the shared set")
        self.assertFalse(self.attached_is(none=True), "the primary is untouched")
        self.assertEqual(self.controller.detaches, 0)
        # the survivors received every chunk, identically
        expected = [payload_for(seq, 16 * KiB) for seq in range(count)]
        for client in (ui, fast_a, fast_b):
            self.assertTrue(wait_for(lambda c=client: len(self.live_payloads(c)) >= count, 30),
                            f"{len(self.live_payloads(client))} of {count}")
            self.assertEqual(self.live_payloads(client), expected)
        # the slow stream: only whole valid frames, ending in exactly one closing slow_client, nothing after it
        frames = slow.drain_until_eof(20)
        types = [f.header.get("type") for f in frames]
        self.assertNotIn("_undecodable", types)
        self.assertEqual(types[-1], "closing", types[-3:])
        self.assertEqual(types.count("closing"), 1)
        self.assertEqual(frames[-1].header.get("reason"), "slow_client")
        got = [f.header["sequence"] for f in frames if f.header.get("type") == "display"]
        self.assertEqual(got, list(range(got[0], got[0] + len(got))), "no hole before the closing")
        self.assertLess(len(got), count)
        self.assertTrue(slow.eof.is_set(), "the server closed that connection")
        # panes/controller unaffected, a fresh Hub connection can take the freed place and gets replay + live
        self.assertTrue(wait_for(lambda: self.controller.detaches == 0))
        fresh = self.shared()
        self.assertEqual(self.shared_count(), 3)
        self.broadcast(b"after")
        self.assertTrue(self.wait_live(fresh, b"after"))
        self.assertTrue(self.wait_live(ui, b"after"))

    def test_a_slow_primary_detaches_alone_and_the_shared_attachments_keep_receiving(self):
        ui = self.raw(UI, read=False, rcvbuf=4096)
        ui.sock.sendall(ui_v1.encode_frame({"v": 1, "type": "attach", "id": "a1"}))
        self.assertTrue(wait_for(lambda: not self.attached_is(none=True), 5))
        hub = self.shared()
        count = self.flood()
        self.assertTrue(self.attached_is(none=True), "the slow primary was detached")
        self.assertEqual(self.controller.detaches, 1)
        self.assertEqual(self.shared_count(), 1, "the shared attachment is not touched by the primary's overflow")
        self.assertTrue(wait_for(lambda: len(self.live_payloads(hub)) >= count, 30))
        self.assertEqual(self.live_payloads(hub), [payload_for(seq, 16 * KiB) for seq in range(count)])
        self.push_state(999)
        self.assertTrue(wait_for(lambda: any(f.header.get("type") == "state" and f.header["snapshot"]["n"] == 999
                                             for f in hub.snapshot_frames()), 5))
        self.tui()  # the primary place is free again


class RequestTests(Fixture):
    def test_focus_and_detach_from_shared_are_not_attached_and_do_not_move_the_ui(self):
        ui = self.tui()
        hub = self.shared()
        focus_before = self.controller.focus
        for kind, fields in ((ClientType.FOCUS, {"pane": "host_shell"}), (ClientType.DETACH, {})):
            result = hub.request(kind, **fields)
            self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
            self.assertIn("primary attach only", result.get("detail", ""))
        self.assertEqual(self.controller.focus, focus_before)
        self.assertNotIn(("focus", "host_shell"), self.controller.events)
        self.assertEqual((self.controller.detaches, self.attached_is(none=True)), (0, False))
        self.assertEqual(self.shared_count(), 1)
        self.broadcast(b"both")
        self.assertTrue(self.wait_live(ui, b"both"))
        self.assertTrue(self.wait_live(hub, b"both"))
        self.assertTrue(ui.request(ClientType.FOCUS, pane="host_shell")["ok"], "the TUI's own focus still works")
        self.assertEqual(self.controller.focus, PaneId.HOST_SHELL)

    def test_focus_and_detach_from_shared_without_a_primary(self):
        hub = self.shared()
        for kind, fields in ((ClientType.FOCUS, {"pane": "host_shell"}), (ClientType.DETACH, {})):
            self.assertEqual(hub.request(kind, **fields).get("reason"), "not_attached")
        self.assertEqual(self.shared_count(), 1)
        self.assertEqual(self.controller.events, [])

    def test_an_unshared_hub_connection_still_cannot_send_pane_requests(self):
        hub = self.raw(HUB)
        for kind, fields, payload in ((ClientType.INPUT, {"pane": "host_shell"}, b"x"),
                                      (ClientType.PASTE, {"pane": "host_shell"}, b"x"),
                                      (ClientType.RESIZE, {"pane": "host_shell", "rows": 24, "cols": 80}, b""),
                                      (ClientType.TAKEOVER_REQUEST, {}, b""), (ClientType.HANDOFF, {}, b""),
                                      (ClientType.RESTART_PANE, {"pane": "worker_omp"}, b""),
                                      (ClientType.KILL_PANE, {"pane": "host_shell"}, b""),
                                      (ClientType.STOP_COMMAND, {"command_id": CID}, b"")):
            with self.subTest(kind=kind.value):
                self.assertEqual(hub.request(kind, payload, **fields).get("reason"), "not_attached")
        self.assertEqual(self.controller.events, [])

    CASES = (
        (ClientType.INPUT, b"typed", {"pane": "host_shell"}),
        (ClientType.INPUT, b"typed", {"pane": "manager_omp"}),
        (ClientType.INPUT, b"typed", {"pane": "worker_omp"}),
        (ClientType.PASTE, b"pasted", {"pane": "host_shell"}),
        (ClientType.PASTE, b"pasted", {"pane": "worker_omp"}),
        *((ClientType.INPUT, b"REFUSE:" + r.encode(), {"pane": "host_shell"}) for r in
          ("host_shell_automation", "input_owner_manager", "input_target_unknown", "pane_unavailable", "queue_full",
           "paste_too_large")),
        (ClientType.PASTE, b"REFUSE:input_owner_manager", {"pane": "host_shell"}),
        (ClientType.PASTE, b"P" * (ui_v1.MAX_PASTE_BYTES + 1), {"pane": "host_shell"}),
        (ClientType.PASTE, b"P" * 4096, {"pane": "host_shell"}),
        (ClientType.TAKEOVER_REQUEST, b"", {}), (ClientType.TAKEOVER_CONFIRM, b"", {}), (ClientType.HANDOFF, b"", {}),
        (ClientType.RESTART_PANE, b"", {"pane": "manager_omp"}), (ClientType.RESTART_PANE, b"", {"pane": "worker_omp"}),
        (ClientType.RESTART_PANE, b"", {"pane": "host_shell"}),
        (ClientType.KILL_PANE, b"", {"pane": "manager_omp"}), (ClientType.KILL_PANE, b"", {"pane": "worker_omp"}),
        (ClientType.KILL_PANE, b"", {"pane": "host_shell"}), (ClientType.KILL_PANE, b"", {"pane": "host_shell"}),
    )

    def run_cases(self, client):
        self.controller.shell_exited = False
        out = []
        for kind, payload, fields in self.CASES:
            out.append(strip(client.request(kind, payload, **fields)))
        return out

    def test_every_allowed_request_answers_exactly_like_the_primary(self):
        ui = self.tui()
        as_primary = self.run_cases(ui)
        ui.request(ClientType.DETACH)
        ui.close()
        shared_alone = self.run_cases(self.shared())
        self.tui()
        shared_with_primary = self.run_cases(self.shared())
        self.assertEqual(shared_alone, as_primary)
        self.assertEqual(shared_with_primary, as_primary)
        reasons = {r.get("reason") for r in as_primary if not r["ok"]}
        for expected in ("host_shell_automation", "input_owner_manager", "input_target_unknown", "pane_unavailable",
                         "queue_full", "paste_too_large", "pane_not_killable", "pane_exited", "pane_alive",
                         "takeover_held", "handoff_held"):
            self.assertIn(expected, reasons)

    def test_a_restart_from_shared_pushes_state_to_the_primary_and_every_shared(self):
        ui = self.tui()
        a, b = self.shared(), self.shared()
        self.controller.state_n = 41
        result = a.request(ClientType.RESTART_PANE, pane="manager_omp")
        self.assertTrue(result["ok"], result)
        for client in (ui, a, b):
            self.assertTrue(wait_for(lambda c=client: any(f.header.get("type") == "state"
                                                          and f.header["snapshot"]["n"] == 41
                                                          for f in c.snapshot_frames()), 5))

    def test_stop_command_ack_and_final_go_to_the_requester_and_the_state_to_everyone(self):
        ui = self.tui()
        a, b = self.shared(), self.shared()
        self.controller.stop_gate.clear()
        a.send({"v": 1, "type": "stop_command", "id": "stop1", "command_id": CID})
        ack = a.result("stop1")
        self.assertEqual((ack["ok"], ack["stop"]["status"]), (True, "stopping"), ack)
        self.assertTrue(self.quiet(b, 0.1))
        self.controller.state_n = 77
        self.controller.stop_gate.set()
        final = a.result("stop1", final=True)
        self.assertEqual((final["stop"]["status"], final["stop"]["requested_by"]), ("exited", "user"), final)
        for client in (ui, a, b):
            self.assertTrue(wait_for(lambda c=client: any(f.header.get("type") == "state"
                                                          and f.header["snapshot"]["n"] == 77
                                                          for f in c.snapshot_frames()), 5))
        for client in (ui, b):
            self.assertFalse([f for f in client.snapshot_frames() if f.header.get("type") == "result"
                              and f.header.get("id") == "stop1"], "the final goes to the requester only")


class SizeTests(Fixture):
    def resizes(self):
        return [e for e in self.controller.events if e[0] == "resize"]

    def test_refused_while_a_primary_is_attached_for_every_pane(self):
        self.tui(size=(30, 100))
        hub = self.shared()
        for pane in PANES:
            result = hub.request(ClientType.RESIZE, pane=pane, rows=40, cols=120)
            self.assertEqual((result["ok"], result.get("reason")), (False, "resize_owned_by_ui"), result)
        self.assertEqual(self.resizes(), [])
        self.assertEqual(self.controller.attaches, [(30, 100)])

    def test_applied_without_a_primary_per_pane_and_the_last_one_wins(self):
        a, b = self.shared(), self.shared()
        self.assertEqual(self.controller.attaches, [], "sharing never resizes")
        self.assertEqual(self.resizes(), [])
        self.assertTrue(a.request(ClientType.RESIZE, pane="host_shell", rows=31, cols=101)["ok"])
        self.assertTrue(b.request(ClientType.RESIZE, pane="host_shell", rows=32, cols=102)["ok"])
        self.assertTrue(a.request(ClientType.RESIZE, pane="worker_omp", rows=33, cols=103)["ok"])
        self.assertTrue(a.request(ClientType.RESIZE, pane="host_shell", rows=34, cols=104)["ok"])
        self.assertEqual(self.resizes(), [("resize", "host_shell", 31, 101), ("resize", "host_shell", 32, 102),
                                          ("resize", "worker_omp", 33, 103), ("resize", "host_shell", 34, 104)])

    def test_pane_is_required_with_or_without_a_primary(self):
        hub = self.shared()
        result = hub.request(ClientType.RESIZE, rows=40, cols=120)
        self.assertEqual((result["ok"], result.get("reason")), (False, "invalid_message"), result)
        self.tui(size=(30, 100))
        result = hub.request(ClientType.RESIZE, rows=40, cols=120)
        self.assertFalse(result["ok"])
        self.assertIn(result.get("reason"), {"invalid_message", "resize_owned_by_ui"})
        self.assertEqual(self.resizes(), [])

    def test_bad_dimensions_are_invalid_and_not_applied(self):
        hub = self.shared()
        for rows, cols in ((0, 80), (24, 0), (-1, 80), (24, 100000), (100000, 80), ("a", 80), (None, 80)):
            result = hub.request(ClientType.RESIZE, pane="host_shell", rows=rows, cols=cols)
            self.assertEqual((result["ok"], result.get("reason")), (False, "invalid_message"), (rows, cols, result))
        self.assertEqual(self.resizes(), [])

    def test_the_primary_keeps_its_size_rules_with_shared_attachments_around(self):
        hub = self.shared()
        hub.request(ClientType.RESIZE, pane="host_shell", rows=50, cols=150)
        ui = self.tui(size=(30, 100))
        self.assertEqual(self.controller.attaches, [(30, 100)])
        self.assertTrue(ui.request(ClientType.RESIZE, rows=28, cols=90)["ok"], "primary resize without a pane")
        self.assertTrue(ui.request(ClientType.RESIZE, pane="host_shell", rows=27, cols=89)["ok"])
        self.assertEqual(self.resizes()[-2:], [("resize", None, 28, 90), ("resize", "host_shell", 27, 89)])
        ui.request(ClientType.DETACH)
        self.assertTrue(hub.request(ClientType.RESIZE, pane="host_shell", rows=60, cols=160)["ok"],
                        "without a primary the web may size again")
        self.assertEqual(self.resizes()[-1], ("resize", "host_shell", 60, 160))

    def test_a_ui_attach_racing_pipelined_shared_resizes_always_wins(self):
        hub = self.shared()
        done = threading.Event()

        def hammer():
            for _ in range(600):
                if done.is_set():
                    break
                try:
                    hub.send({"v": 1, "type": "resize", "id": "h", "pane": "host_shell", "rows": 77, "cols": 177})
                except OSError:
                    break
        thread = threading.Thread(target=hammer, daemon=True)
        thread.start()
        time.sleep(0.01)
        ui = self.tui(size=(30, 100))
        mark = self.in_loop(lambda: len(self.controller.events))
        time.sleep(0.3)
        done.set()
        thread.join(10)
        self.assertTrue(wait_for(lambda: True, 0.2))
        events = list(self.controller.events)
        attach_at = events.index(("attach", (30, 100)))
        after = [e for e in events[attach_at + 1:] if e[0] == "resize"]
        self.assertEqual(after, [], "no web resize applied after the UI attached")
        refused = [f for f in hub.snapshot_frames() if f.header.get("reason") == "resize_owned_by_ui"]
        self.assertTrue(refused, "the later web resizes were answered resize_owned_by_ui")
        ui.request(ClientType.SNAPSHOT)
        self.assertGreaterEqual(mark, attach_at)


class ReleaseTests(Fixture):
    def test_share_detach_stops_everything_and_the_place_is_reusable(self):
        ui = self.tui()
        hub = self.shared()
        self.broadcast(b"one")
        self.assertTrue(self.wait_live(hub, b"one"))
        result = hub.request(ClientType.SHARE_DETACH)
        self.assertEqual((result["ok"], result["detached"]), (True, True), result)
        self.assertEqual(self.shared_count(), 0)
        mark = len(hub.snapshot_frames())
        self.broadcast(b"two")
        self.push_state(3)
        self.assertTrue(self.wait_live(ui, b"two"))
        self.assertTrue(self.quiet(hub))
        self.assertEqual(len(hub.snapshot_frames()), mark, "nothing at all is sent after share_detach")
        self.assertEqual(hub.request(ClientType.INPUT, b"x", pane="host_shell").get("reason"), "not_attached")
        self.assertEqual(hub.request(ClientType.SHARE_DETACH).get("reason"), "not_attached")
        self.assertEqual(hub.request(ClientType.RESIZE, pane="host_shell", rows=9, cols=9).get("reason"),
                         "not_attached")
        self.assertTrue(hub.request(ClientType.SHARE_ATTACH)["ok"])
        replay = [f for f in hub.snapshot_frames() if f.header.get("replay")]
        self.assertTrue(replay, "re-share replays again")
        self.assertEqual(self.controller.detaches, 0)
        self.assertEqual(self.controller.attaches, [None])

    def test_each_way_out_frees_exactly_one_place_and_never_touches_the_primary(self):
        ui = self.tui()
        hubs = [self.shared() for _ in range(4)]
        self.assertEqual(self.shared_count(), 4)
        hubs[0].request(ClientType.SHARE_DETACH)
        hubs[1].sock.shutdown(socket.SHUT_RDWR)
        hubs[1].sock.close()
        hubs[2].sock.sendall(b"garbage-that-is-not-a-frame" * 20)  # protocol error closes it
        self.assertTrue(wait_for(lambda: self.shared_count() == 1, 5), self.shared_count())
        self.assertFalse(self.attached_is(none=True))
        self.assertEqual((self.controller.detaches, self.controller.attaches), (0, [None]))
        again = [self.shared() for _ in range(3)]
        self.assertEqual(self.shared_count(), 4)
        self.assertEqual(self.raw(HUB).request(ClientType.SHARE_ATTACH).get("reason"), "queue_full")
        self.broadcast(b"live")
        for client in (ui, hubs[3], *again):
            self.assertTrue(self.wait_live(client, b"live"))

    def test_a_closed_shared_connection_does_not_leave_a_ghost_in_the_fan_out(self):
        hub = self.shared()
        hub.sock.shutdown(socket.SHUT_RDWR)
        self.assertTrue(wait_for(lambda: self.shared_count() == 0, 5))
        self.broadcast(b"nobody")  # must not raise on a dead connection
        self.push_state(1)
        self.assertEqual(self.in_loop(lambda: len(self.server.connections)), 0)


class TuiUnchangedTests(Fixture):
    def script(self, with_shared):
        """One TUI flow with fixed ids; returns every frame the TUI received, normalised."""
        if with_shared:
            hub = self.shared()
        controller = self.controller
        controller.history.clear()
        ui = self.raw(UI)
        ui.send({"v": 1, "type": "attach", "id": "A", "size": {"rows": 30, "cols": 100}})
        ui.result("A")
        self.broadcast(b"hello")
        self.push_state(1)
        for rid, fields, payload in (("F", {"type": "focus", "pane": "worker_omp"}, b""),
                                     ("I", {"type": "input", "pane": "worker_omp"}, b"keys"),
                                     ("R", {"type": "resize", "rows": 31, "cols": 101}, b""),
                                     ("D", {"type": "detach"}, b"")):
            ui.send({"v": 1, "id": rid, **fields}, payload)
            ui.result(rid)
        self.broadcast(b"after-detach")  # nobody is attached... except the shared one
        ui.send({"v": 1, "type": "attach", "id": "A2"})
        ui.result("A2")
        self.broadcast(b"again")
        self.assertTrue(self.wait_live(ui, b"again"))
        time.sleep(0.1)
        frames = ui.snapshot_frames()
        keep = [f for f in frames if f.header.get("type") != "welcome"]
        return [(f.header.get("type"), json.dumps({k: v for k, v in f.header.items() if k not in {"session_id"}},
                                                  sort_keys=True), f.payload) for f in keep], list(controller.events)

    def test_the_tui_conversation_is_byte_identical_with_and_without_a_shared_attachment(self):
        plain = self.script(False)
        # restart with a fresh server/controller for the second run
        self.tearDown()
        self.setUp()
        shared = self.script(True)
        self.assertEqual(shared[0], plain[0])
        self.assertEqual(shared[1], plain[1], "the controller saw the same attach/detach/focus/resize calls")

    def test_a_second_primary_is_attached_elsewhere_and_the_tui_detach_keeps_the_shared_place(self):
        hub = self.shared()
        ui = self.tui()
        other = self.raw(UI)
        refused = other.request(ClientType.ATTACH)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "attached_elsewhere"), refused)
        self.assertTrue(ui.request(ClientType.DETACH)["detached"])
        self.assertEqual((self.shared_count(), self.controller.detaches), (1, 1))
        self.broadcast(b"only-shared")
        self.assertTrue(self.wait_live(hub, b"only-shared"))
        self.assertTrue(self.quiet(ui))
        mark = len([f for f in hub.snapshot_frames() if f.header.get("replay")])
        second = other.request(ClientType.ATTACH)  # the free place now goes to another UI client
        self.assertTrue(second["ok"], second)
        self.assertEqual(len([f for f in hub.snapshot_frames() if f.header.get("replay")]), mark,
                         "a primary attach replays to the primary only")



# =====================================================================================================================
# Part B: a real Backend (fake OMPs, real host shell PTY), real UiClient connections
# =====================================================================================================================
from command_stop_support import pids_running  # noqa: E402
from test_command_stop_authority import BackendStopFixture  # noqa: E402


class RealFixture(BackendStopFixture):
    def setUp(self):
        super().setUp()
        self.backend.ui.hub_peer = lambda pid: pid == os.getpid()  # this process plays the verified Hub

    def off_loop(self, call, what):
        box: dict = {}

        def run():
            try:
                box["value"] = call()
            except BaseException as exc:
                box["error"] = exc
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.wait(lambda: not thread.is_alive(), what)
        if "error" in box:
            raise box["error"]
        return box["value"]

    def connect(self, name):
        client = self.off_loop(lambda: UiClient(self.backend.layout.ui_socket, name=name, timeout=10), "welcome")
        self.addCleanup(client.close)
        return client

    def request(self, client, kind, payload=b"", **fields):
        return self.off_loop(lambda: client.request(kind, payload, **fields), kind.value)

    def tui(self, size=(30, 100)):
        ui = self.connect(UI)
        result = self.request(ui, ClientType.ATTACH, size={"rows": size[0], "cols": size[1]})
        self.assertTrue(result["ok"], result)
        return ui

    def shared(self):
        hub = self.connect(HUB)
        result = self.request(hub, ClientType.SHARE_ATTACH)
        self.assertTrue(result["ok"], result)
        return hub

    def pump_until(self, clients, predicate, what, timeout=40.0):
        def check():
            for client in clients:
                client.pump(0)
            return predicate()
        self.wait(check, what, timeout)

    def settle(self, clients, ticks=40):
        for _ in range(ticks):
            self.backend._tick(0.02)
            for client in clients:
                client.pump(0)

    def pane(self, name):
        return self.backend.snapshot()["panes"][name]

    def wait_prompt(self):
        self.wait(lambda: self.pane("host_shell")["shell"]["parent_mode"] == "manual_prompt", "the shell prompt")

    @staticmethod
    def displays(client):
        out: dict[str, list] = {}
        for f in client.displays:
            h = f.header
            out.setdefault(h["pane"], []).append((h["sequence"], bool(h.get("replay")), h["session_id"],
                                                  h["generation"], f.payload))
        return out

    def snapshot_counts(self):
        s = self.backend.snapshot()
        return s["attached"], s["shared_attachments"]


class RealSnapshotTests(RealFixture):
    def test_shared_attachments_is_exact_after_every_kind_of_exit(self):
        self.assertEqual(self.snapshot_counts(), (False, 0))
        ui = self.tui()
        a, b, c, d = (self.shared() for _ in range(4))
        self.assertEqual(self.snapshot_counts(), (True, 4))
        self.assertEqual(self.request(self.connect(HUB), ClientType.SHARE_ATTACH).get("reason"), "queue_full")
        self.assertEqual(self.request(a, ClientType.SHARE_DETACH)["detached"], True)
        b.sock.shutdown(socket.SHUT_RDWR)
        c.sock.sendall(b"not a frame at all" * 50)
        self.wait(lambda: self.snapshot_counts() == (True, 1), "the three exits", 10)
        d.sock.shutdown(socket.SHUT_RDWR)
        self.wait(lambda: self.snapshot_counts() == (True, 0), "the last shared exit", 10)
        ui.sock.shutdown(socket.SHUT_RDWR)
        self.wait(lambda: self.snapshot_counts() == (False, 0), "the primary exit", 10)
        server = self.backend.ui
        self.assertEqual((server.shared, server.attached), ([], None))
        self.assertTrue(self.wait_closed_stats())
        self.shared()
        self.assertEqual(self.snapshot_counts(), (False, 1), "and the places are reusable")

    def wait_closed_stats(self):
        stats = self.backend.ui.stats
        self.wait(lambda: stats["accepted"] - stats["closed"] == len(self.backend.ui.connections), "stats")
        return True

    def test_attached_keeps_meaning_the_primary_and_the_ui_sees_a_state_with_the_count(self):
        hub = self.shared()
        ui = self.tui()
        self.assertEqual(self.snapshot_counts(), (True, 1))
        self.pump_until([ui, hub], lambda: bool(ui.states and hub.states), "state pushes")
        self.assertTrue(ui.states[-1]["attached"] and hub.states[-1]["attached"])
        self.assertEqual((ui.states[-1]["shared_attachments"], hub.states[-1]["shared_attachments"]), (1, 1))
        self.assertTrue(self.backend.snapshot()["attached"])


class RealFanOutTests(RealFixture):
    def test_identical_run_of_bytes_live_and_an_unbroken_replay_to_live_boundary_for_a_late_joiner(self):
        ui = self.tui()
        early = self.shared()
        self.wait_prompt()
        command = b"yes abcdefghijklmnopqrstuvwxyz0123456789 | head -c 1500000; echo DONE$((600+14))\r"
        self.assertTrue(self.request(ui, ClientType.INPUT, command, pane="host_shell")["ok"])
        self.pump_until([ui, early], lambda: any(f.header.get("pane") == "host_shell" and b"1500000" not in f.payload
                                                 for f in early.displays) and
                        sum(len(f.payload) for f in early.displays if f.header["pane"] == "host_shell") > 300_000,
                        "output flowing")
        late = self.connect(HUB)
        # request on a thread while this thread keeps the loop (and the other clients) running
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(late.request(ClientType.SHARE_ATTACH)), daemon=True)
        thread.start()
        self.pump_until([ui, early], lambda: bool(result), "the late share_attach answer")
        thread.join(5)
        self.assertTrue(result["ok"], result)
        everyone = [ui, early, late]
        self.pump_until(everyone, lambda: all(b"DONE614" in b"".join(p for *_, p in self.displays(c).get("host_shell", []))
                                              for c in everyone), "the command's end on every attachment")
        self.settle(everyone)
        views = [self.displays(c) for c in everyone]
        for pane in PANES:
            by_seq = [{seq: payload for seq, _, _, _, payload in v.get(pane, [])} for v in views]
            runs = []
            for v in views:
                items = v.get(pane, [])
                seqs = [i[0] for i in items]
                self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))) if seqs else [],
                                 f"{pane}: replay + live is one unbroken run (no gap, no duplicate)")
                flags = [i[1] for i in items]
                self.assertEqual(flags, sorted(flags, reverse=True), f"{pane}: replay frames strictly before live")
                self.assertEqual(len({(i[2], i[3]) for i in items}), 1, f"{pane}: one session/generation")
                runs.append(seqs)
            for index in (1, 2):
                for seq, payload in by_seq[index].items():
                    if seq in by_seq[0]:
                        self.assertEqual(payload, by_seq[0][seq], f"{pane} seq {seq} differs from the primary's")
            self.assertEqual(runs[0][-1:], runs[1][-1:])
            self.assertEqual(runs[0][-1:], runs[2][-1:])
        # live frames of the two attachments that joined before the output are identical in bytes and order
        live = lambda c: [(f.header["sequence"], f.payload) for f in c.displays
                          if f.header["pane"] == "host_shell" and not f.header.get("replay")]
        ui_live, early_live = live(ui), live(early)
        start = early_live[0][0]
        self.assertEqual([x for x in ui_live if x[0] >= start], early_live)
        late_first_live = min(f.header["sequence"] for f in late.displays
                              if f.header["pane"] == "host_shell" and not f.header.get("replay"))
        late_last_replay = max((f.header["sequence"] for f in late.displays
                                if f.header["pane"] == "host_shell" and f.header.get("replay")), default=None)
        self.assertEqual(late_first_live, (late_last_replay or 0) + 1 if late_last_replay else late_first_live)
        self.assertIsNotNone(self.backend.ui.attached)

    def test_state_pushes_after_an_action_are_the_same_for_the_ui_and_the_hub(self):
        ui, hub = self.tui(), self.shared()
        self.assertTrue(self.request(hub, ClientType.KILL_PANE, pane="host_shell")["ok"])
        self.pump_until([ui, hub], lambda: all(any(not s["panes"]["host_shell"]["alive"] for s in c.states)
                                               for c in (ui, hub)), "the kill seen by both")
        first = [next(s for s in c.states if not s["panes"]["host_shell"]["alive"]) for c in (ui, hub)]
        self.assertEqual(first[0], first[1])


class RealSizeTests(RealFixture):
    def sizes(self):
        return {name: (p["rows"], p["cols"]) for name, p in self.backend.snapshot()["panes"].items()}

    def test_the_ui_owns_the_size_while_attached_and_the_last_web_resize_wins_otherwise(self):
        hub, hub2 = self.shared(), self.shared()
        self.assertTrue(self.request(hub, ClientType.RESIZE, pane="host_shell", rows=40, cols=120)["ok"])
        self.assertTrue(self.request(hub2, ClientType.RESIZE, pane="host_shell", rows=41, cols=121)["ok"])
        self.assertEqual(self.sizes()["host_shell"], (41, 121))
        self.assertTrue(self.request(hub, ClientType.RESIZE, pane="worker_omp", rows=42, cols=122)["ok"])
        untouched = self.sizes()["manager_omp"]
        self.assertNotEqual(untouched, (42, 122))
        before = self.sizes()
        ui = self.tui((30, 100))
        self.assertEqual(set(self.sizes().values()), {(30, 100)}, "the UI's attach sizes every pane")
        for pane in PANES:
            refused = self.request(hub, ClientType.RESIZE, pane=pane, rows=50, cols=150)
            self.assertEqual((refused["ok"], refused.get("reason")), (False, "resize_owned_by_ui"), refused)
        self.assertEqual(set(self.sizes().values()), {(30, 100)})
        nopane = self.request(hub, ClientType.RESIZE, rows=50, cols=150)
        self.assertFalse(nopane["ok"])
        self.assertIn(nopane.get("reason"), {"invalid_message", "resize_owned_by_ui"})
        self.assertTrue(self.request(ui, ClientType.RESIZE, rows=33, cols=111)["ok"])
        self.assertEqual(set(self.sizes().values()), {(33, 111)})
        ui.sock.shutdown(socket.SHUT_RDWR)
        self.wait(lambda: self.backend.ui.attached is None, "the UI gone", 10)
        self.assertEqual(set(self.sizes().values()), {(33, 111)}, "the UI leaving does not resize")
        self.assertTrue(self.request(hub2, ClientType.RESIZE, pane="manager_omp", rows=44, cols=144)["ok"])
        self.assertEqual(self.sizes()["manager_omp"], (44, 144))
        self.assertEqual(self.sizes()["worker_omp"], (33, 111))
        self.tui((29, 99))
        self.assertEqual(set(self.sizes().values()), {(29, 99)}, "a returning UI takes the size back")
        self.assertEqual(before["worker_omp"], (42, 122))


class RealOwnerTests(RealFixture):
    def test_one_owner_for_the_ui_and_the_hub_whoever_hands_off_or_takes_over(self):
        ui, hub = self.tui(), self.shared()
        self.wait_prompt()
        self.assertTrue(self.request(ui, ClientType.INPUT, b"wb-handoff\r", pane="host_shell")["ok"])
        self.wait(lambda: self.pane("host_shell")["shell"]["parent_mode"] == "control_wait", "control wait")
        confirmed = [self.request(c, ClientType.TAKEOVER_CONFIRM) for c in (ui, hub)]
        self.assertEqual(strip(confirmed[0]), strip(confirmed[1]), "the same answer from either side")
        handed = self.request(ui, ClientType.HANDOFF)  # the TUI hands the shell to the manager
        self.assertEqual((handed["ok"], handed["shell"]["input_owner"]), (True, "manager"), handed)
        self.pump_until([ui, hub], lambda: all(c.states and c.states[-1]["panes"]["host_shell"]["input_owner"]
                                               == "manager" for c in (ui, hub)), "owner manager on both")
        refused = [self.request(c, ClientType.INPUT, b"echo no\r", pane="host_shell") for c in (hub, ui)]
        self.assertEqual([(r["ok"], r.get("reason")) for r in refused], [(False, "input_owner_manager")] * 2)
        self.assertEqual(strip(refused[0]), strip(refused[1]))
        taken = self.request(hub, ClientType.TAKEOVER_REQUEST)  # the hub takes over
        self.assertEqual((taken["ok"], taken["shell"]["input_owner"]), (True, "user"), taken)
        self.pump_until([ui, hub], lambda: all(c.states[-1]["panes"]["host_shell"]["input_owner"] == "user"
                                               for c in (ui, hub)), "owner user on both")
        self.wait_prompt()
        # "user" is one owner: the UI's input is accepted right after the Hub took over, and vice versa
        self.assertTrue(self.request(ui, ClientType.INPUT, b"echo from-ui-615\r", pane="host_shell")["ok"])
        self.assertTrue(self.request(hub, ClientType.INPUT, b"echo from-hub-615\r", pane="host_shell")["ok"])
        text = lambda c: b"".join(f.payload for f in c.displays if f.header["pane"] == "host_shell")
        out_ui, out_hub = b"\rfrom-ui-615\r\n", b"\rfrom-hub-615\r\n"
        self.pump_until([ui, hub], lambda: all(out_ui in text(c) and out_hub in text(c) for c in (ui, hub)),
                        "both outputs on both attachments")
        positions = [text(c).find(out_ui) < text(c).find(out_hub) for c in (ui, hub)]
        self.assertEqual(positions, [True, True], "arrival order is the order on both")

    def test_refusals_for_takeover_handoff_are_the_same_from_either_side(self):
        ui, hub = self.tui(), self.shared()
        self.wait_prompt()
        for kind in (ClientType.HANDOFF, ClientType.TAKEOVER_CONFIRM):
            answers = [self.request(c, kind) for c in (ui, hub)]
            self.assertEqual(strip(answers[0]), strip(answers[1]), kind)


class RealInputTests(RealFixture):
    def test_lone_ctrl_c_is_forwarded_and_journaled_once_from_the_hub_with_and_without_a_ui(self):
        hub = self.shared()
        self.open_free_task()
        records = []
        for label, with_ui in (("sleep 616.1", False), ("sleep 616.2", True)):
            ui = self.tui() if with_ui else None
            started = self.run_command(label)
            self.wait(lambda: self.pane("host_shell")["input_owner"] == "manager", "the command holds the shell")
            before = len(self.records("command_interrupt"))
            answer = self.request(hub, ClientType.INPUT, b"\x03", pane="host_shell")
            self.assertEqual((answer["ok"], answer["accepted_bytes"]), (True, 1), answer)
            self.wait(lambda: len(self.records("command_interrupt")) == before + 1, "the journal record")
            self.settle([], 10)
            new = self.records("command_interrupt")[before:]
            self.assertEqual(len(new), 1, "exactly one record per Ctrl+C")
            self.assertEqual((new[0]["requested_by"], new[0]["command_id"], new[0]["delivered"]),
                             ("user", started["command_id"], True), new[0])
            records.append(new[0])
            self.wait(lambda: not self.backend.terminal.current().get("running"), "the command ended", 20)
            if ui is not None:
                ui.close()
        self.assertEqual({k for k in records[0] if k not in {"pid", "start_ticks", "process_group", "at",
                                                              "command_id"}},
                         {k for k in records[1] if k not in {"pid", "start_ticks", "process_group", "at",
                                                              "command_id"}})
        self.assertEqual([r["input_owner"] for r in records], ["manager", "manager"])

    def test_a_ctrl_c_that_is_not_lone_or_comes_as_a_paste_is_refused_like_the_ui_and_not_journaled(self):
        ui, hub = self.tui(), self.shared()
        self.open_free_task()
        started = self.run_command("sleep 616.3")
        self.wait(lambda: self.pane("host_shell")["input_owner"] == "manager", "the command holds the shell")
        for kind, data in ((ClientType.PASTE, b"\x03"), (ClientType.INPUT, b"\x03\x03"), (ClientType.INPUT, b"a\x03")):
            answers = [self.request(c, kind, data, pane="host_shell") for c in (ui, hub)]
            self.assertEqual(strip(answers[0]), strip(answers[1]), (kind, data))
            self.assertEqual((answers[0]["ok"], answers[0].get("reason")), (False, "input_owner_manager"))
        self.assertEqual(self.records("command_interrupt"), [], "nothing was interrupted or journaled")
        self.assertTrue(pids_running("sleep", "616.3"), "the command still runs")
        self.assertTrue(self.request(hub, ClientType.INPUT, b"\x03", pane="host_shell")["ok"])
        self.wait(lambda: bool(self.records("command_interrupt")), "the lone Ctrl+C record")
        self.assertEqual(self.records("command_interrupt")[0]["command_id"], started["command_id"])

    def test_pane_state_refusals_match_the_ui_after_a_kill_and_omp_panes_are_not_killable(self):
        ui, hub = self.tui(), self.shared()
        for pane in ("manager_omp", "worker_omp"):
            answers = [self.request(c, ClientType.KILL_PANE, pane=pane) for c in (ui, hub)]
            self.assertEqual([a.get("reason") for a in answers], ["pane_not_killable"] * 2)
            self.assertEqual(strip(answers[0]), strip(answers[1]))
            self.assertEqual(self.pane(pane)["alive"], True, "the OMP is not signalled")
        self.assertTrue(self.request(hub, ClientType.KILL_PANE, pane="host_shell")["ok"])
        again = [self.request(c, ClientType.KILL_PANE, pane="host_shell") for c in (ui, hub)]
        self.assertEqual([a.get("reason") for a in again], ["pane_exited"] * 2)
        gone = [self.request(c, ClientType.INPUT, b"x", pane="host_shell") for c in (ui, hub)]
        self.assertEqual(strip(gone[0]), strip(gone[1]))
        self.assertEqual(gone[0].get("reason"), "pane_unavailable")
        alive_restart = [self.request(c, ClientType.RESTART_PANE, pane="worker_omp") for c in (ui, hub)]
        self.assertEqual([a.get("reason") for a in alive_restart], ["pane_alive"] * 2)
        self.assertTrue(self.request(hub, ClientType.RESTART_PANE, pane="host_shell")["ok"])
        self.pump_until([ui, hub], lambda: all(c.states and c.states[-1]["panes"]["host_shell"]["alive"] for c in
                                               (ui, hub)), "both see the restarted shell")

    def test_omp_input_and_paste_from_the_hub_are_accepted_like_the_ui(self):
        ui, hub = self.tui(), self.shared()
        for pane in ("manager_omp", "worker_omp"):
            for kind in (ClientType.INPUT, ClientType.PASTE):
                answers = [self.request(c, kind, b"hello", pane=pane) for c in (ui, hub)]
                self.assertEqual(strip(answers[0]), strip(answers[1]))
                self.assertTrue(answers[0]["ok"])

    def test_stop_command_from_the_hub_finishes_for_the_hub_and_the_ui_only_sees_the_state(self):
        ui, hub = self.tui(), self.shared()
        self.open_free_task()
        started = self.run_command("trap '' INT; sleep 617.3")
        hub.send_frame({"v": 1, "type": "stop_command", "id": "sc1", "command_id": started["command_id"]})
        got: list = []

        def collect():
            for frame in list(hub.pending):
                if frame.header.get("type") == "result" and frame.header.get("id") == "sc1" and frame not in got:
                    got.append(frame)
            return any(f.header.get("final") for f in got)
        self.pump_until([hub, ui], collect, "the final", 40)
        ack = next(f.header for f in got if not f.header.get("final"))
        final = next(f.header for f in got if f.header.get("final"))
        self.assertEqual((ack["stop"]["status"], final["stop"]["status"], final["stop"]["requested_by"]),
                         ("stopping", "exited", "user"))
        self.pump_until([hub, ui], lambda: ui.states and ui.states[-1]["panes"]["host_shell"]["command"] is None,
                        "the UI sees the command gone")
        self.assertFalse([f for f in ui.pending if f.header.get("type") == "result"
                          and f.header.get("id") == "sc1"], "the UI never gets the Hub's stop result")
        self.assertEqual([r["requested_by"] for r in self.records("command_stop_requested")], ["user"])


class RealSlowSharedTests(RealFixture):
    def test_a_hub_that_stops_reading_is_closed_alone_and_the_ui_and_panes_run_on(self):
        self.backend.ui.max_outbound = 1 * MiB
        ui = self.tui()
        hub = self.shared()  # never pumped again: it does not read
        self.wait_prompt()
        command = b"yes abcdefghijklmnopqrstuvwxyz0123456789 | head -c 6000000; echo DONE$((600+15))\r"
        self.assertTrue(self.request(ui, ClientType.INPUT, command, pane="host_shell")["ok"])
        self.pump_until([ui], lambda: b"DONE615" in b"".join(f.payload for f in ui.displays
                                                              if f.header["pane"] == "host_shell"),
                        "the UI receiving the whole output", 90)
        self.assertEqual(self.snapshot_counts(), (True, 0), "only the hub left")
        self.assertIsNone(ui.closing)
        self.assertEqual(self.backend.ui.stats["detaches"], 0)
        snapshot = self.backend.snapshot()
        for name in PANES:
            self.assertTrue(snapshot["panes"][name]["alive"], name)
        self.assertEqual(snapshot["phase"], "ready")
        # the hub's stream ends in a whole closing slow_client
        deadline = time.monotonic() + 20
        while hub.closing is None and time.monotonic() < deadline:
            self.backend._tick(0.02)
            hub.pump(0)
        self.assertIsNotNone(hub.closing)
        self.assertEqual(hub.closing.get("reason"), "slow_client")
        self.assertTrue(self.request(ui, ClientType.INPUT, b"echo still-616\r", pane="host_shell")["ok"])
        self.pump_until([ui], lambda: b"still-616" in b"".join(f.payload for f in ui.displays[-20:]), "echo after")
        self.assertEqual(self.request(self.connect(HUB), ClientType.SHARE_ATTACH)["ok"], True)


class RealTuiUnchangedTests(RealFixture):
    def test_second_primary_attached_elsewhere_and_detach_attach_with_a_shared_attachment_around(self):
        hub = self.shared()
        ui = self.tui()
        other = self.connect(UI)
        refused = self.request(other, ClientType.ATTACH)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "attached_elsewhere"), refused)
        self.assertTrue(self.request(ui, ClientType.DETACH)["detached"])
        self.assertEqual(self.snapshot_counts(), (False, 1))
        hub_replays = sum(1 for f in hub.displays if f.header.get("replay"))
        again = self.request(other, ClientType.ATTACH, size={"rows": 31, "cols": 101})
        self.assertTrue(again["ok"], again)
        self.assertEqual(self.snapshot_counts(), (True, 1))
        self.assertEqual(sum(1 for f in hub.displays if f.header.get("replay")), hub_replays,
                         "the Hub gets no replay for the UI's attach")
        self.assertTrue(any(f.header.get("replay") for f in other.displays), "the UI gets its own replay")
        self.assertEqual(set(p["rows"] for p in self.backend.snapshot()["panes"].values()), {31})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
