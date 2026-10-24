"""HB-06 (HB-T02): ui_v1 ``pause``/``resume`` from a connection that did not attach.

Seam S5: a real ``UiServer`` on a unix socket with a fake controller (the pattern of ``test_ui_server.py``) and the
real ``UiClient``. A peer uid other than the backend's is injected with a ``UiServer`` subclass that overrides the
method reading the connection's peer identity (``peer_identity``); the real socket's peer is always this test's uid.

Expected values are this file's own, taken from the SPEC contract text ("ui_v1 변경: attach 없는 pause/resume"): a
non-attached connection whose hello ``client`` is ``workbench-hub`` and whose server-side peer uid equals the backend
euid gets the same controller ``pause``/``resume`` as an attached UI; anything else is ``not_attached`` and the
controller is not called; the request takes no attach slot; an attached UI keeps its attachment and gets the ``state``
push. The controller call record is the evidence that the backend behaviour ran (or did not).
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from test_ui_server import FakeController
from workbench.backend.client import UiClient
from workbench.backend.ui_server import Held, UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Reason
from workbench.contracts.v1 import PaneId

HUB = "workbench-hub"


class AutomationController(FakeController):
    """Records pause/resume calls; ``resume`` refuses like ``Backend.resume`` (service.py) does."""

    def __init__(self):
        super().__init__()
        self.pauses = 0
        self.resumes: list[object] = []
        self.paused = False
        # FakeController.restart_pane/kill_pane append to these (they start empty here, so a call is visible)
        self.restarts: list[PaneId] = []
        self.kills: list[PaneId] = []

    def snapshot(self):
        return {**super().snapshot(), "automation": {"paused": self.paused}}

    def pause(self):
        self.pauses += 1
        self.paused = True
        return {"automation": {"state": "pausing", "paused": True}}

    def resume(self, reconciled):
        self.resumes.append(reconciled)
        if reconciled is not True:
            raise Held(Reason.RESUME_NOT_RECONCILED, "resume needs reconciled: true")
        self.paused = False
        return {"automation": {"state": "resuming", "paused": False}}


class ForeignUidServer(UiServer):
    """Every accepted peer looks like another user (the injection point of seam S5)."""

    def peer_identity(self, sock):
        pid, uid = super().peer_identity(sock)
        return pid, uid + 1


class UnreadablePeerServer(UiServer):
    """The peer credentials cannot be read for any accepted connection."""

    def peer_identity(self, sock):
        raise OSError("no peer credentials")


class HubControlFixture(unittest.TestCase):
    server_class = UiServer

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="hb06-ui-")
        self.path = Path(self._dir.name) / "ui.sock"
        self.controller = AutomationController()
        self.server = self.server_class(self.path, self.controller)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        # The server is touched only from this thread; tests read its plain attributes (attached, connections).
        while not self.stop.is_set():
            self.server.poll(0.01)

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

    def attached_ui(self):
        ui = self.client(name="workbench-ui")
        self.assertTrue(ui.attach()["ok"])
        ui.pump(0.05)
        return ui

    def wait_for_state(self, client, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(predicate(state) for state in client.states):
                return True
            client.pump(0.05)
        return False


class NonAttachedHubPauseTests(HubControlFixture):
    def test_hub_pause_without_attach_is_accepted_and_calls_the_controller_once(self):
        hub = self.client()
        result = hub.request(ClientType.PAUSE)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["automation"]["paused"], True)
        self.assertEqual(self.controller.pauses, 1)
        self.assertIsNone(self.server.attached)
        self.assertEqual(self.controller.attaches, [])

    def test_other_client_names_without_attach_are_not_attached_and_the_controller_is_not_called(self):
        for name in ("workbench-cli", "workbench-ui", "workbench-min-client", "workbench-hub ", "Workbench-Hub",
                     "workbench-hub-x", ""):
            with self.subTest(name=name):
                other = self.client(name=name)
                for kind, fields in ((ClientType.PAUSE, {}), (ClientType.RESUME, {"reconciled": True})):
                    result = other.request(kind, **fields)
                    self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual((self.controller.pauses, self.controller.resumes), (0, []))

    def test_an_attached_ui_stays_attached_and_gets_the_state_push_of_a_hub_pause(self):
        ui = self.attached_ui()
        attached = self.server.attached
        ui.states.clear()
        hub = self.client()
        result = hub.request(ClientType.PAUSE)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.controller.pauses, 1)
        self.assertTrue(self.wait_for_state(ui, lambda state: state.get("automation") == {"paused": True}),
                        ui.states)
        self.assertIs(self.server.attached, attached)
        self.assertEqual((len(self.controller.attaches), self.controller.detaches), (1, 0))
        hub.pump(0.2)
        self.assertEqual(hub.states, [], "a state push goes to the attached UI only")
        # the attached UI still has its input path
        self.assertTrue(ui.input(PaneId.MANAGER_OMP, b"x")["ok"])

    def test_hub_resume_without_attach_follows_the_reconciled_rule(self):
        ui = self.attached_ui()
        hub = self.client()
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        ui.states.clear()
        refused = hub.request(ClientType.RESUME, reconciled=False)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "resume_not_reconciled"), refused)
        self.assertEqual(self.controller.resumes, [False])
        resumed = hub.request(ClientType.RESUME, reconciled=True)
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["automation"]["paused"], False)
        self.assertEqual(self.controller.resumes, [False, True])
        self.assertTrue(self.wait_for_state(ui, lambda state: state.get("automation") == {"paused": False}),
                        ui.states)
        self.assertEqual((len(self.controller.attaches), self.controller.detaches), (1, 0))

    def test_a_hub_request_takes_no_attach_slot_so_a_new_ui_attaches_afterwards(self):
        hub = self.client()
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertTrue(hub.request(ClientType.RESUME, reconciled=True)["ok"])
        self.assertIsNone(self.server.attached)
        ui = self.client(name="workbench-ui")
        self.assertTrue(ui.attach()["ok"])
        self.assertEqual(len(self.controller.attaches), 1)
        # the hub connection, still open and still not attached, keeps working beside it
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertEqual(self.controller.pauses, 2)

    def test_hub_pause_right_after_the_attached_ui_left_is_accepted(self):
        ui = self.attached_ui()
        ui.close()
        deadline = time.monotonic() + 3
        while self.server.attached is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(self.server.attached)
        hub = self.client()
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertEqual(self.controller.pauses, 1)

    def test_other_attach_requiring_requests_stay_not_attached_for_a_non_attached_hub(self):
        hub = self.client()
        requests = [
            (ClientType.DETACH, b"", {}),
            (ClientType.INPUT, b"x", {"pane": "manager_omp"}),
            (ClientType.PASTE, b"x", {"pane": "host_shell"}),
            (ClientType.RESIZE, b"", {"pane": "manager_omp", "rows": 24, "cols": 80}),
            (ClientType.FOCUS, b"", {"pane": "worker_omp"}),
            (ClientType.RESTART_PANE, b"", {"pane": "manager_omp"}),
            (ClientType.KILL_PANE, b"", {"pane": "host_shell"}),
            (ClientType.STOP_COMMAND, b"", {"command_id": "6f1b6a3e-2f40-4a3f-9d4e-0c1b2a3d4e5f"}),
            (ClientType.TAKEOVER_REQUEST, b"", {}),
            (ClientType.TAKEOVER_CONFIRM, b"", {}),
            (ClientType.HANDOFF, b"", {}),
        ]
        for kind, payload, fields in requests:
            with self.subTest(kind=kind.value):
                result = hub.request(kind, payload, **fields)
                self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        # the controller record: no input/paste delivered, no restart/kill, focus unchanged
        self.assertEqual((self.controller.frames, self.controller.restarts, self.controller.kills,
                          self.controller.focus),
                         ({pane: [] for pane in PaneId}, [], [], PaneId.MANAGER_OMP))

    def test_a_hub_cannot_take_the_attach_slot_of_an_attached_ui(self):
        ui = self.attached_ui()
        attached = self.server.attached
        hub = self.client()
        refused = hub.request(ClientType.ATTACH)
        self.assertEqual((refused["ok"], refused.get("reason")), (False, "attached_elsewhere"), refused)
        self.assertIs(self.server.attached, attached)
        self.assertTrue(ui.input(PaneId.MANAGER_OMP, b"y")["ok"])

    def test_pause_before_hello_is_refused_and_the_connection_closed(self):
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(5)
        sock.connect(str(self.path))
        self.addCleanup(sock.close)
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p1", "client": HUB}))
        decoder, frames = ui_v1.FrameDecoder(), []
        while not frames:
            data = sock.recv(65536)
            if not data:
                break
            frames.extend(decoder.feed(data))
        self.assertEqual(frames[0].header.get("reason"), "hello_required", frames[0].header)
        self.assertEqual(self.controller.pauses, 0)

    def test_pause_with_a_wrong_frame_version_is_refused_and_the_controller_not_called(self):
        hub = self.client()
        hub.send_frame({"v": 2, "type": "pause", "id": "p2"})
        frame = hub.next_frame()
        self.assertEqual(frame.header.get("reason"), "version_mismatch", frame.header)
        self.assertEqual(self.controller.pauses, 0)

    def test_the_server_records_the_real_peer_identity_of_each_connection(self):
        hub = self.client()
        hub.snapshot()
        connection, = self.server.connections
        self.assertEqual(connection.peer, (os.getpid(), os.geteuid()))


class ForeignPeerTests(HubControlFixture):
    server_class = ForeignUidServer

    def test_hub_pause_and_resume_from_another_uid_are_not_attached_and_the_controller_is_not_called(self):
        hub = self.client()
        for kind, fields in ((ClientType.PAUSE, {}), (ClientType.RESUME, {"reconciled": True})):
            result = hub.request(kind, **fields)
            self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual((self.controller.pauses, self.controller.resumes), (0, []))


class UnreadablePeerTests(HubControlFixture):
    server_class = UnreadablePeerServer

    def test_an_unreadable_peer_identity_is_not_attached(self):
        hub = self.client()
        result = hub.request(ClientType.PAUSE)
        self.assertEqual((result["ok"], result.get("reason")), (False, "not_attached"), result)
        self.assertEqual(self.controller.pauses, 0)


if __name__ == "__main__":
    unittest.main()
