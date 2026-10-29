"""WBX-E1: the Hub's shared attach on the backend ui_v1 server (``share_attach``/``share_detach``).

Seam: the backend ui_server seam (Hub SPEC S5): a real ``UiServer`` on a unix socket with the ``FakeController`` of
``test_ui_server.py`` and the real ``UiClient``; the peer identity is injected with a ``UiServer`` subclass that
overrides ``peer_identity`` and the verified-Hub check is the ``hub_peer`` callable the backend passes.

Expected values are this file's own, from web-board-ux SPEC s5 "ui_v1 덧붙이기: 공유 attach": a connection becomes a
shared attachment only for hello name ``workbench-hub`` + the backend's uid + the verified running Hub pid, else
``not_attached`` with the attach slot unchanged; it coexists with the primary attach (a second primary is still
``attached_elsewhere``); display and state fan out to every attachment; a shared attachment may not ``focus`` or
``detach``; its ``resize`` needs a ``pane`` and is refused with ``resize_owned_by_ui`` while a primary is attached.
"""

from __future__ import annotations

import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest

from test_ui_server import FakeController
from workbench.backend.client import UiClient
from workbench.backend.ui_server import UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId

HUB = "workbench-hub"
UI = "workbench-ui"
MiB = 1024 * 1024


class PeerServer(UiServer):
    """``uid_offset`` makes every later peer look like another uid; ``unreadable`` makes it unreadable."""

    uid_offset = 0
    unreadable = False

    def peer_identity(self, sock):
        if self.unreadable:
            raise OSError("no peer credentials")
        pid, uid = super().peer_identity(sock)
        return pid, uid + self.uid_offset


class ShareFixture(unittest.TestCase):
    max_outbound = 8 * MiB

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="wbx-e1-")
        self.path = Path(self._dir.name) / "ui.sock"
        self.controller = FakeController()
        self.hub_pids = {os.getpid()}  # the verified running Hub's pid (this test process plays the Hub)
        self.checked: list[int] = []
        self.server = PeerServer(self.path, self.controller, max_outbound=self.max_outbound, hub_peer=self._is_hub)
        self.calls: queue.SimpleQueue = queue.SimpleQueue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _is_hub(self, pid):
        self.checked.append(pid)
        return pid in self.hub_pids

    def _serve(self):
        # The server is touched only on this thread; the test runs its own server calls here too (``in_loop``).
        while not self.stop.is_set():
            self.server.poll(0.01)
            while True:
                try:
                    call, done, box = self.calls.get_nowait()
                except queue.Empty:
                    break
                try:
                    box.append(call())
                except BaseException as exc:  # handed back to the test
                    box.append(exc)
                done.set()

    def in_loop(self, call):
        done, box = threading.Event(), []
        self.calls.put((call, done, box))
        self.assertTrue(done.wait(5), "the server loop did not run the call")
        if isinstance(box[0], BaseException):
            raise box[0]
        return box[0]

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.server.close(flush_timeout=0.5)
        self.assertFalse(self.path.exists())
        self._dir.cleanup()

    def client(self, name=HUB):
        client = UiClient(self.path, name=name, timeout=10)
        self.addCleanup(client.close)
        return client

    def tui(self, size=None):
        ui = self.client(UI)
        self.assertTrue(ui.attach(size)["ok"])
        return ui

    def shared(self):
        hub = self.client(HUB)
        result = hub.request(ClientType.SHARE_ATTACH)
        self.assertTrue(result["ok"], result)
        return hub

    def broadcast(self, data):
        def send():
            chunk = self.controller.chunk(data)
            self.server.broadcast(chunk)
            return chunk
        return self.in_loop(send)

    def push_state(self, snapshot):
        self.in_loop(lambda: self.server.push_state(snapshot))

    @staticmethod
    def live(client):
        return [ui_v1.decode_display(f).data for f in client.displays if not f.header.get("replay")]

    def wait_live(self, client, data, timeout=3.0):
        deadline = time.monotonic() + timeout
        while data not in self.live(client) and time.monotonic() < deadline:
            client.pump(0.05)
        return data in self.live(client)

    def quiet(self, client, seconds=0.2):
        """Frames that reached ``client`` within ``seconds`` (none expected)."""
        before = (len(client.displays), len(client.states), len(client.pending))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            client.pump(0.05)
        return (len(client.displays), len(client.states), len(client.pending)) != before


class AdmissionTests(ShareFixture):
    """(2) The name + uid + verified Hub pid gate; any miss is ``not_attached`` and the attach slot is unchanged."""

    def test_the_verified_hub_becomes_a_shared_attachment_without_taking_the_attach_slot(self):
        hub = self.client(HUB)
        result = hub.request(ClientType.SHARE_ATTACH)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.server.attached)
        self.assertEqual(self.controller.attaches, [], "a shared attach is not the UI's on_attach")
        self.assertIn(os.getpid(), self.checked, "the Hub process check ran")
        self.broadcast(b"live-1")
        self.assertTrue(self.wait_live(hub, b"live-1"))

    def refused(self, hub, ui=None):
        ui = ui or self.tui()
        attached = self.server.attached
        result = hub.request(ClientType.SHARE_ATTACH)
        self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertIn("Workbench Hub", result.get("detail", ""))
        self.assertIs(self.server.attached, attached)
        marker = f"after-refusal-{id(hub)}".encode()
        self.broadcast(marker)
        self.assertTrue(self.wait_live(ui, marker))
        self.assertFalse(self.quiet(hub), "a refused connection gets nothing")
        self.assertEqual(hub.request(ClientType.SHARE_DETACH).get("reason"), "not_attached")

    def test_another_hello_name_is_not_attached(self):
        ui = self.tui()
        for name in (UI, "workbench-hub2", "workbench-cli", ""):
            with self.subTest(name=name):
                self.refused(self.client(name), ui)

    def test_another_peer_uid_is_not_attached(self):
        self.server.uid_offset = 1
        hub = self.client(HUB)
        self.server.uid_offset = 0
        self.refused(hub)

    def test_an_unreadable_peer_is_not_attached(self):
        self.server.unreadable = True
        hub = self.client(HUB)
        self.server.unreadable = False
        self.refused(hub)

    def test_a_pid_that_is_not_the_verified_hub_is_not_attached(self):
        self.hub_pids = set()
        self.refused(self.client(HUB))
        self.assertIn(os.getpid(), self.checked)

    def test_a_failing_hub_check_is_not_attached(self):
        def broken(pid):
            raise RuntimeError("hub.json unreadable")
        self.server.hub_peer = broken
        self.refused(self.client(HUB))


class CoexistenceTests(ShareFixture):
    """(3) Shared attachments coexist with the primary; the primary rules are unchanged; at most 4 shared."""

    def test_a_shared_attachment_and_the_primary_coexist_and_a_second_primary_is_still_attached_elsewhere(self):
        ui = self.tui((30, 100))
        primary = self.server.attached
        hub = self.shared()
        self.assertIs(self.server.attached, primary)
        self.assertEqual(self.controller.attaches, [(30, 100)])
        other = self.client(UI)
        refused = other.attach()
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "attached_elsewhere"), refused)
        self.assertIs(self.server.attached, primary)
        self.assertEqual(self.controller.detaches, 0)
        self.broadcast(b"both")
        self.assertTrue(self.wait_live(ui, b"both"))
        self.assertTrue(self.wait_live(hub, b"both"))

    def test_a_shared_connection_cannot_attach_and_cannot_share_twice(self):
        hub = self.shared()
        for kind in (ClientType.ATTACH, ClientType.SHARE_ATTACH):
            with self.subTest(kind=kind.value):
                refused = hub.request(kind)
                self.assertEqual((refused["ok"], refused.get("reason")), (False, "invalid_message"), refused)
        self.assertIsNone(self.server.attached)
        self.assertEqual(self.controller.attaches, [])
        ui = self.tui()  # the slot is still free for the UI
        self.broadcast(b"once")
        self.assertTrue(self.wait_live(hub, b"once"))
        self.assertTrue(self.wait_live(ui, b"once"))
        self.assertEqual(self.live(hub).count(b"once"), 1, "the shared connection is in the set once")

    def test_the_primary_connection_cannot_share_attach(self):
        primary = self.client(HUB)  # a Hub-named connection that took the primary slot (allowed as before)
        self.assertTrue(primary.attach()["ok"])
        refused = primary.request(ClientType.SHARE_ATTACH)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "invalid_message"), refused)
        self.assertIs(self.server.attached, self.server.connections[0])
        self.broadcast(b"single")
        self.assertTrue(self.wait_live(primary, b"single"))
        self.quiet(primary)
        self.assertEqual(self.live(primary).count(b"single"), 1)

    def test_at_most_four_shared_attachments_and_a_freed_place_is_reusable(self):
        hubs = [self.shared() for _ in range(4)]
        fifth = self.client(HUB)
        refused = fifth.request(ClientType.SHARE_ATTACH)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "queue_full"), refused)
        self.assertTrue(hubs[0].request(ClientType.SHARE_DETACH)["detached"])
        self.assertTrue(fifth.request(ClientType.SHARE_ATTACH)["ok"])
        ui = self.tui()  # the primary is not counted against the shared limit
        self.broadcast(b"all")
        for client in (ui, fifth, *hubs[1:]):
            self.assertTrue(self.wait_live(client, b"all"))


class OrderTests(ShareFixture):
    """(4) ``share_attach`` is answered like ``attach``: result + snapshot, then each pane's replay, then live."""

    def raw_hub(self):
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(5)
        sock.connect(str(self.path))
        self.addCleanup(sock.close)
        sock.sendall(ui_v1.encode_frame(ui_v1.hello(HUB)))
        return sock, ui_v1.FrameDecoder()

    @staticmethod
    def frames(sock, decoder, count):
        found = []
        while len(found) < count:
            found.extend(decoder.feed(sock.recv(65536)))
        return found

    def test_result_and_snapshot_then_replay_then_live_without_gap_or_duplicate(self):
        self.broadcast(b"before")  # nobody attached: not delivered, and not replayed by the fake either
        sock, decoder = self.raw_hub()
        (welcome,) = self.frames(sock, decoder, 1)
        self.assertEqual(welcome.header["type"], "welcome")
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "share_attach", "id": "s1"}))
        result, replay = self.frames(sock, decoder, 2)
        self.assertEqual((result.header["type"], result.header["id"], result.header["ok"]), ("result", "s1", True))
        self.assertEqual(result.header["snapshot"], self.controller.snapshot())
        self.assertEqual((replay.header["type"], replay.header.get("replay"), replay.payload),
                         ("display", True, b"retained"))
        sent = [self.broadcast(data) for data in (b"live-a", b"live-b")]
        live = self.frames(sock, decoder, 2)
        self.assertEqual([(f.header.get("replay"), f.payload, f.header["sequence"]) for f in live],
                         [(None, c.data, c.sequence) for c in sent])


class FanOutTests(ShareFixture):
    """(5) Display and state reach the primary and every shared attachment as the same frames."""

    def wait_states(self, client, count, timeout=3.0):
        deadline = time.monotonic() + timeout
        while len(client.states) < count and time.monotonic() < deadline:
            client.pump(0.05)
        return client.states

    def test_display_and_state_frames_are_identical_on_the_primary_and_every_shared_attachment(self):
        ui = self.tui()
        hubs = [self.shared(), self.shared()]
        chunk = self.broadcast(b"\x1b[1mfan-out\xed\x95\x9c")
        self.push_state({"focus": "worker_omp", "n": 1})
        for client in (ui, *hubs):
            self.assertTrue(self.wait_live(client, chunk.data))
            self.assertEqual(self.wait_states(client, 1), [{"focus": "worker_omp", "n": 1}])
        encoded = {tuple(ui_v1.encode_frame(f.header, f.payload) for f in c.displays if not f.header.get("replay"))
                   for c in (ui, *hubs)}
        self.assertEqual(encoded, {(ui_v1.encode_display(chunk),)})

    def test_a_shared_attachment_alone_gets_display_and_state(self):
        hub = self.shared()
        self.broadcast(b"alone")
        self.push_state({"n": 2})
        self.assertTrue(self.wait_live(hub, b"alone"))
        self.assertEqual(self.wait_states(hub, 1), [{"n": 2}])

    def test_a_connection_that_did_not_attach_gets_neither(self):
        hub, plain = self.shared(), self.client(HUB)
        self.broadcast(b"only-attached")
        self.push_state({"n": 3})
        self.assertTrue(self.wait_live(hub, b"only-attached"))
        self.assertFalse(self.quiet(plain))


class SlowSharedClientTests(ShareFixture):
    """(6) A shared attachment that does not read is closed alone with ``slow_client``; nothing else is touched."""

    max_outbound = 256 * 1024

    def test_overflow_closes_only_that_shared_connection_and_frees_its_place(self):
        ui = self.tui()
        reader, slow = self.shared(), self.shared()
        slow_connection = self.in_loop(lambda: self.server.shared[-1])
        block = b"z" * (60 * 1024)
        for index in range(400):
            chunk = self.broadcast(block + str(index).encode())
            for client in (ui, reader):
                self.assertTrue(self.wait_live(client, chunk.data), index)
                client.displays.clear()
            if self.in_loop(lambda: slow_connection.closing):
                break
        self.assertTrue(self.in_loop(lambda: slow_connection.closing), "the slow connection never overflowed")
        self.assertIsNotNone(self.server.attached)
        self.assertEqual(self.controller.detaches, 0, "the primary was not detached")
        after = self.broadcast(b"after-overflow")
        for client in (ui, reader):
            self.assertTrue(self.wait_live(client, after.data))
        self.assertTrue(ui.input(PaneId.MANAGER_OMP, b"still-typing")["ok"])
        # the slow connection's stream is whole frames ending with closing slow_client
        deadline = time.monotonic() + 10
        while slow.closing is None and time.monotonic() < deadline:
            if not slow.pump(0.1):
                break
            slow.displays.clear()
        self.assertEqual((slow.closing or {}).get("reason"), "slow_client")
        # its shared place is free again: three more Hub connections fit next to the reader
        for _ in range(3):
            self.shared()


STOP_ID = "6f1b6a3e-2f40-4a3f-9d4e-0c1b2a3d4e5f"


def answer(result):
    """A result without its request id, for comparing what two connections were told."""
    return {k: v for k, v in result.items() if k != "id"}


class RequestTests(ShareFixture):
    """(7) A shared attachment may not ``focus``/``detach``; its allowed requests get the primary's handling."""

    def setUp(self):
        super().setUp()
        self.controller.stop_command = lambda command_id: {"status": "exited", "command_id": command_id}

    def test_focus_and_detach_are_primary_only(self):
        ui, hub = self.tui(), self.shared()
        for kind, fields in ((ClientType.FOCUS, {"pane": "worker_omp"}), (ClientType.DETACH, {})):
            with self.subTest(kind=kind.value):
                refused = hub.request(kind, **fields)
                self.assertEqual((refused["ok"], refused.get("reason")), (False, "not_attached"), refused)
                self.assertIn("primary attach only", refused.get("detail", ""))
        self.assertEqual(self.controller.focus, PaneId.MANAGER_OMP)
        self.assertEqual(self.controller.detaches, 0)
        self.broadcast(b"still-shared")
        self.assertTrue(self.wait_live(hub, b"still-shared"), "a refused detach did not end the shared attach")
        self.assertTrue(ui.request(ClientType.FOCUS, pane="worker_omp")["ok"])

    def test_input_paste_and_shell_and_pane_requests_answer_like_the_primary(self):
        ui, hub = self.tui(), self.shared()
        same = [(ClientType.INPUT, b"a", {"pane": "host_shell"}),
                (ClientType.PASTE, b"bb", {"pane": "worker_omp"}),
                (ClientType.INPUT, b"c" * (2 * MiB + 1), {"pane": "manager_omp"}),  # paste_too_large
                (ClientType.TAKEOVER_REQUEST, b"", {}),
                (ClientType.TAKEOVER_CONFIRM, b"", {}),  # takeover_held
                (ClientType.HANDOFF, b"", {}),  # handoff_held
                (ClientType.RESTART_PANE, b"", {"pane": "manager_omp"}),
                (ClientType.RESTART_PANE, b"", {"pane": "worker_omp"}),  # pane_alive
                (ClientType.KILL_PANE, b"", {"pane": "manager_omp"})]  # pane_not_killable
        for kind, payload, fields in same:
            with self.subTest(kind=kind.value, fields=fields):
                from_hub = hub.request(kind, payload, timeout=30, **fields)
                from_ui = ui.request(kind, payload, timeout=30, **fields)
                self.assertEqual(answer(from_hub), answer(from_ui))
        self.assertEqual(self.controller.frames[PaneId.HOST_SHELL], [b"a", b"a"])
        self.assertEqual(self.controller.frames[PaneId.WORKER_OMP], [b"bb", b"bb"])
        self.assertEqual(self.controller.frames[PaneId.MANAGER_OMP], [])
        self.controller.shell_owner = "manager"  # the one backend owner holds both
        for client in (hub, ui):
            held = client.input(PaneId.HOST_SHELL, b"x")
            self.assertEqual((held["ok"], held.get("reason")), (False, "input_owner_manager"))
        killed = hub.request(ClientType.KILL_PANE, pane="host_shell")
        self.assertEqual((killed["ok"], killed["killed"]), (True, True), killed)
        again = ui.request(ClientType.KILL_PANE, pane="host_shell")
        self.assertEqual((again["ok"], again.get("reason")), (False, "pane_exited"), "the same pane state")

    def test_a_successful_restart_from_a_shared_attachment_pushes_state_to_every_attachment(self):
        ui, hub = self.tui(), self.shared()
        self.assertTrue(hub.request(ClientType.RESTART_PANE, pane="manager_omp")["ok"])
        for client in (ui, hub):
            deadline = time.monotonic() + 3
            while not client.states and time.monotonic() < deadline:
                client.pump(0.05)
            self.assertEqual(client.states, [self.controller.snapshot()])

    def test_stop_command_final_goes_to_the_requester_and_the_state_push_to_every_attachment(self):
        hub, other = self.shared(), self.shared()  # no primary
        ack = hub.request(ClientType.STOP_COMMAND, command_id=STOP_ID)
        self.assertEqual((ack["ok"], ack["stop"]["status"]), (True, "stopping"), ack)
        final = hub.next_frame()
        self.assertEqual((final.header["id"], final.header.get("final"), final.header["stop"]["status"]),
                         (ack["id"], True, "exited"))
        for client in (hub, other):
            deadline = time.monotonic() + 3
            while not client.states and time.monotonic() < deadline:
                client.pump(0.05)
            self.assertEqual(client.states, [self.controller.snapshot()])
        self.assertEqual(other.pending, [], "the other attachment did not get the requester's final")


class SizeTests(ShareFixture):
    """(8) A shared ``resize`` names a pane; it applies only while no primary is attached."""

    def size(self):
        return getattr(self.controller, "size", None)

    def test_while_a_primary_is_attached_a_shared_resize_is_refused_and_no_pane_changes(self):
        ui, hub = self.tui((40, 120)), self.shared()
        refused = hub.request(ClientType.RESIZE, pane="host_shell", rows=10, cols=20)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "resize_owned_by_ui"), refused)
        self.assertIsNone(self.size())
        self.assertTrue(ui.request(ClientType.RESIZE, rows=41, cols=121)["ok"], "the primary resize is unchanged")
        self.assertEqual(self.size(), (None, 41, 121))
        self.assertEqual(self.controller.attaches, [(40, 120)])

    def test_without_a_primary_a_shared_resize_applies_to_its_pane_and_the_last_one_wins(self):
        first, second = self.shared(), self.shared()
        applied = first.request(ClientType.RESIZE, pane="worker_omp", rows=10, cols=20)
        self.assertEqual((applied["ok"], applied.get("reason")), (True, None), applied)
        self.assertEqual(self.size(), (PaneId.WORKER_OMP, 10, 20))
        self.assertTrue(second.request(ClientType.RESIZE, pane="worker_omp", rows=11, cols=21)["ok"])
        self.assertEqual(self.size(), (PaneId.WORKER_OMP, 11, 21))
        ui = self.tui((30, 90))  # a primary that attaches afterwards owns the size again
        self.assertEqual(self.controller.attaches, [(30, 90)])
        refused = first.request(ClientType.RESIZE, pane="worker_omp", rows=12, cols=22)
        self.assertEqual(refused.get("reason"), "resize_owned_by_ui")
        self.assertEqual(self.size(), (PaneId.WORKER_OMP, 11, 21))
        self.assertTrue(ui.detach()["detached"])
        self.assertTrue(first.request(ClientType.RESIZE, pane="worker_omp", rows=12, cols=22)["ok"])
        self.assertEqual(self.size(), (PaneId.WORKER_OMP, 12, 22))

    def test_a_shared_resize_without_a_pane_is_an_invalid_message_with_or_without_a_primary(self):
        hub = self.shared()
        for with_primary in (False, True):
            if with_primary:
                self.tui()
            with self.subTest(with_primary=with_primary):
                refused = hub.request(ClientType.RESIZE, rows=10, cols=20)
                self.assertEqual((refused["ok"], refused.get("reason")), (False, "invalid_message"), refused)
                self.assertIsNone(self.size())


class ShareDetachTests(ShareFixture):
    """(9) After ``share_detach`` (or closing) nothing more is sent and the place is free."""

    def test_after_share_detach_nothing_is_sent_and_the_connection_may_share_again(self):
        ui, hub = self.tui(), self.shared()
        done = hub.request(ClientType.SHARE_DETACH)
        self.assertEqual((done["ok"], done["detached"]), (True, True), done)
        self.broadcast(b"after-detach")
        self.push_state({"n": 9})
        self.assertTrue(self.wait_live(ui, b"after-detach"))
        self.assertFalse(self.quiet(hub), "a detached shared connection gets nothing")
        self.assertIs(self.server.attached, self.server.connections[0], "the primary is untouched")
        self.assertEqual(hub.request(ClientType.SHARE_DETACH).get("reason"), "not_attached")
        self.assertTrue(hub.request(ClientType.SHARE_ATTACH)["ok"])

    def test_closed_shared_connections_free_their_places(self):
        for _ in range(3):
            hubs = [self.shared() for _ in range(4)]
            for hub in hubs:
                hub.close()
            deadline = time.monotonic() + 3
            while self.in_loop(lambda: len(self.server.connections)) and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(self.in_loop(lambda: len(self.server.connections)), 0)
        self.assertEqual(len([self.shared() for _ in range(4)]), 4)


class TuiUnchangedTests(ShareFixture):
    """(13) With a shared attachment present the UI's attach, replay, input, detach and re-attach are as before."""

    def test_the_ui_flow_is_unchanged_next_to_a_shared_attachment(self):
        hub = self.shared()
        ui, second = self.client(UI), self.client(UI)
        self.assertEqual(ui.paste(PaneId.MANAGER_OMP, b"x")["reason"], "not_attached")
        attached = ui.attach((24, 80))
        self.assertTrue(attached["ok"])
        self.assertEqual(attached["snapshot"], self.controller.snapshot())
        self.assertEqual(self.controller.attaches, [(24, 80)])
        self.assertEqual(second.attach()["reason"], "attached_elsewhere")
        ui.pump(0.2)
        self.assertEqual([ui_v1.decode_display(f).data for f in ui.displays if f.header.get("replay")], [b"retained"])
        self.assertTrue(ui.input(PaneId.HOST_SHELL, b"typed")["ok"])
        self.assertTrue(ui.request(ClientType.FOCUS, pane="worker_omp")["ok"])
        self.assertEqual(self.controller.focus, PaneId.WORKER_OMP)
        self.assertTrue(ui.request(ClientType.RESIZE, rows=50, cols=160)["ok"])
        self.assertEqual(self.controller.size, (None, 50, 160))
        self.assertEqual(ui.detach()["detached"], True)
        self.assertEqual(self.controller.detaches, 1)
        self.assertTrue(second.attach()["ok"])
        self.broadcast(b"after-reattach")
        self.assertTrue(self.wait_live(second, b"after-reattach"))
        self.assertTrue(self.wait_live(hub, b"after-reattach"), "the shared attachment rode through it all")
        self.assertFalse(self.quiet(ui), "the detached UI gets nothing")


if __name__ == "__main__":
    unittest.main()
