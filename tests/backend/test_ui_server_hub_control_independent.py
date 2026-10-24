"""HB-06 independent verification: ui_v1 ``pause``/``resume`` from a connection that did not attach.

Seam S5: a real ``UiServer`` on a unix socket, a recording fake controller and real UDS connections (the pattern of
``test_ui_server.py``). Every expectation below comes from the contract text only (SPEC "ui_v1 변경: attach 없는
pause/resume", ticket HB-06, the ``ui_v1`` docstring), not from the implementation's own tests:

* a connection that did not attach may send ``pause``/``resume`` and nothing else that needs attaching, and only when
  its hello ``client`` is exactly ``workbench-hub`` and the server-read peer uid equals the backend euid;
* anything else is ``not_attached`` and the controller is not touched;
* the request takes no attach slot, an attached UI stays attached and gets the ``state`` push (the requester gets none);
* ``resume`` needs ``reconciled: true``.

The server-side peer identity is injected through the ``peer_identity`` override point of ``UiServer`` (ticket
"Peer 신원 읽기는 UiServer의 작은 메서드로 두어 테스트 하위 클래스가 덮어쓸 수 있게 한다").
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
from workbench.backend.client import ClientError, UiClient
from workbench.backend.ui_server import Held, UiServer
from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Reason
from workbench.contracts.v1 import PaneId

HUB = "workbench-hub"
EUID = os.geteuid()
STOP_ID = "11111111-2222-4333-8444-555555555555"

# Everything in ClientType that is not answered before the attach check, i.e. what a hub name must NOT unlock.
ATTACH_REQUIRED = {
    ClientType.DETACH: {},
    ClientType.INPUT: {"pane": PaneId.HOST_SHELL.value},
    ClientType.PASTE: {"pane": PaneId.HOST_SHELL.value},
    ClientType.RESIZE: {"pane": PaneId.HOST_SHELL.value, "rows": 24, "cols": 80},
    ClientType.FOCUS: {"pane": PaneId.HOST_SHELL.value},
    ClientType.TAKEOVER_REQUEST: {},
    ClientType.TAKEOVER_CONFIRM: {},
    ClientType.HANDOFF: {},
    ClientType.RESTART_PANE: {"pane": PaneId.WORKER_OMP.value},
    ClientType.KILL_PANE: {"pane": PaneId.HOST_SHELL.value},
    ClientType.STOP_COMMAND: {"command_id": STOP_ID},
}
_PAYLOAD = {ClientType.INPUT: b"x", ClientType.PASTE: b"x"}


class SpyController(FakeController):
    """Records which controller methods ran; ``pause``/``resume`` mirror ``Backend`` (refuse non-reconciled resume)."""

    GUARDED = ("pause", "resume", "admit", "resize", "set_focus", "takeover_request", "takeover_confirm", "handoff",
               "restart_pane", "kill_pane", "stop_command", "important_archive_close", "important_delete_db",
               "on_attach", "on_detach")

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []
        self.paused = False
        self.fail_pause = False
        for name in self.GUARDED:
            original = getattr(self, name, None)
            if original is not None:
                setattr(self, name, self._spy(name, original))

    def _spy(self, name, original):
        def wrapper(*args, **kwargs):
            self.calls.append(name)
            return original(*args, **kwargs)
        return wrapper

    def snapshot(self):
        return {**super().snapshot(), "paused": self.paused}

    def pause(self):
        if self.fail_pause:
            raise RuntimeError("boom")
        self.paused = True
        return {"automation": {"paused": True}}

    def resume(self, reconciled):
        if reconciled is not True:
            raise Held(Reason.RESUME_NOT_RECONCILED, "needs reconciled")
        self.paused = False
        return {"automation": {"paused": False}}

    def stop_command(self, *args, **kwargs):  # pragma: no cover - only reached by a broken gate
        return {"stopped": True}

    def control_calls(self):
        return [c for c in self.calls if c not in {"on_attach", "on_detach"}]


class ForeignUidServer(UiServer):
    """Every peer appears as another uid."""

    uid = EUID + 1

    def peer_identity(self, sock):
        pid, _uid = super().peer_identity(sock)
        return pid, self.uid


class UnreadablePeerServer(UiServer):
    def peer_identity(self, sock):
        raise OSError("no SO_PEERCRED")


class FirstForeignServer(UiServer):
    """First accepted connection looks foreign, later ones are the real peer: identity is per connection."""

    seen = 0

    def peer_identity(self, sock):
        pid, uid = super().peer_identity(sock)
        FirstForeignServer.seen += 1
        return (pid, EUID + 1) if FirstForeignServer.seen == 1 else (pid, uid)


class Fixture(unittest.TestCase):
    server_class = UiServer

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="hb06i-ui-")
        self.path = Path(self._dir.name) / "ui.sock"
        self.controller = SpyController()
        self.server = self.server_class(self.path, self.controller, max_outbound=8 * 1024 * 1024)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            with self.lock:
                self.server.poll(0.005)
            time.sleep(0.001)  # let a test thread take the (non-fair) lock between polls

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.server.close(flush_timeout=0.5)
        self._dir.cleanup()

    # -- helpers ---------------------------------------------------------
    def client(self, name):
        client = UiClient(self.path, timeout=10, name=name)
        self.addCleanup(client.close)
        return client

    def raw(self):
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(5)
        sock.connect(str(self.path))
        self.addCleanup(sock.close)
        return sock

    @staticmethod
    def frames(sock, count=1, timeout=5):
        decoder, out = ui_v1.FrameDecoder(), []
        deadline = time.monotonic() + timeout
        while len(out) < count and time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            out.extend(decoder.feed(data))
        return out

    def raw_hello(self, name=HUB, *, extra=None):
        sock = self.raw()
        header = ui_v1.hello(name)
        header.update(extra or {})
        sock.sendall(ui_v1.encode_frame(header))
        welcome, = self.frames(sock)
        self.assertEqual(welcome.header["type"], "welcome")
        return sock

    def attached(self):
        with self.lock:
            return self.server.attached

    def wait_for(self, predicate, what, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if predicate():
                    return
            time.sleep(0.005)
        self.fail(f"timed out waiting for {what}")

    def attach_ui(self, name="workbench-ui"):
        ui = self.client(name)
        self.assertTrue(ui.attach()["ok"])
        return ui

    @staticmethod
    def settle(client):
        """Round trip on `client`: anything the server pushed to it earlier is already in client.states."""
        client.snapshot()

    def assert_refused(self, header, reason="not_attached"):
        self.assertFalse(header["ok"], header)
        self.assertEqual(header["reason"], reason, header)


class UnattachedHubTests(Fixture):
    # ---- the permitted path -------------------------------------------------
    def test_hub_pause_and_resume_without_any_ui(self):
        hub = self.client(HUB)
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertTrue(self.controller.paused)
        self.assertTrue(hub.request(ClientType.RESUME, reconciled=True)["ok"])
        self.assertFalse(self.controller.paused)
        self.assertEqual(self.controller.control_calls(), ["pause", "resume"])
        self.assertIsNone(self.attached())
        self.assertEqual(self.controller.calls.count("on_attach"), 0)

    def test_result_carries_the_controller_fields(self):
        hub = self.client(HUB)
        self.assertEqual(hub.request(ClientType.PAUSE)["automation"], {"paused": True})
        self.assertEqual(hub.request(ClientType.RESUME, reconciled=True)["automation"], {"paused": False})

    def test_resume_requires_reconciled_true(self):
        hub = self.client(HUB)
        self.assert_refused(hub.request(ClientType.RESUME, reconciled=False), "resume_not_reconciled")
        # the same refusal an attached UI gets for the missing / non-boolean forms
        ui = self.attach_ui()
        expected = {}
        for label, fields in (("missing", {}), ("string", {"reconciled": "true"}), ("int", {"reconciled": 1}),
                              ("null", {"reconciled": None})):
            expected[label] = ui.request(ClientType.RESUME, **fields)
            self.assertFalse(expected[label]["ok"], label)
        self.controller.calls.clear()
        for label, fields in (("missing", {}), ("string", {"reconciled": "true"}), ("int", {"reconciled": 1}),
                              ("null", {"reconciled": None})):
            got = hub.request(ClientType.RESUME, **fields)
            self.assertFalse(got["ok"], label)
            self.assertEqual(got["reason"], expected[label]["reason"], label)
        self.assertEqual(self.controller.control_calls(), [])
        self.assertFalse(self.controller.paused is None)

    def test_refused_resume_does_not_change_state_or_push(self):
        ui = self.attach_ui()
        hub = self.client(HUB)
        hub.request(ClientType.PAUSE)
        self.wait_pushes(ui, 1)
        before = len(ui.states)
        self.assert_refused(hub.request(ClientType.RESUME, reconciled=False), "resume_not_reconciled")
        self.settle(ui)
        self.assertEqual(len(ui.states), before)
        self.assertTrue(self.controller.paused)

    def wait_pushes(self, ui, count, timeout=5):
        deadline = time.monotonic() + timeout
        while len(ui.states) < count and time.monotonic() < deadline:
            ui.pump(0.05)
        self.assertGreaterEqual(len(ui.states), count)

    # ---- who may do it ------------------------------------------------------
    def test_other_client_names_stay_not_attached(self):
        for name in ("workbench-cli", "workbench-ui", "workbench-min-client", "", "hub", "workbench-hub-evil"):
            with self.subTest(name=name):
                client = self.client(name)
                self.assert_refused(client.request(ClientType.PAUSE))
                self.assert_refused(client.request(ClientType.RESUME, reconciled=True))
        self.assertEqual(self.controller.control_calls(), [])
        self.assertFalse(self.controller.paused)

    def test_look_alike_names_do_not_match(self):
        names = ["Workbench-Hub", "WORKBENCH-HUB", " workbench-hub", "workbench-hub ", "workbench-hub\n",
                 "workbench-hub\x00", "workbench‑hub", "workbench-hub​", "workbench-hu", "workbench-hubb",
                 "workbench_hub", "workbench-hub,workbench-ui"]
        for name in names:
            with self.subTest(name=name):
                client = self.client(name)
                self.assert_refused(client.request(ClientType.PAUSE))
                self.assert_refused(client.request(ClientType.RESUME, reconciled=True))
        self.assertEqual(self.controller.control_calls(), [])

    def test_missing_client_name_in_hello_is_not_hub(self):
        sock = self.raw()
        header = ui_v1.hello(HUB)
        del header["client"]
        sock.sendall(ui_v1.encode_frame(header))
        self.assertEqual(self.frames(sock)[0].header["type"], "welcome")
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p1"}))
        reply, = self.frames(sock)
        self.assert_refused(reply.header)
        self.assertEqual(self.controller.control_calls(), [])

    def test_non_string_client_name_never_reaches_the_gate(self):
        for value in (["workbench-hub"], {"n": HUB}, 1, True):
            with self.subTest(value=value):
                sock = self.raw()
                sock.sendall(ui_v1.encode_frame({"type": "hello", "versions": [1], "client": value}))
                frames = self.frames(sock)
                if frames and frames[0].header.get("type") == "welcome":
                    sock.sendall(ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p"}))
                    reply = self.frames(sock)
                    self.assertTrue(reply and reply[0].header.get("ok") is False, reply)
        self.assertEqual(self.controller.control_calls(), [])
        with self.client(HUB) as probe:  # server still serves
            self.assertTrue(probe.snapshot())

    def test_client_name_is_fixed_by_the_first_hello(self):
        sock = self.raw_hello("workbench-ui")
        # a second hello is a protocol error and ends that connection; the pause pipelined behind it gets nothing
        sock.sendall(ui_v1.encode_frame(ui_v1.hello(HUB)) +
                     ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p2"}))
        frames = self.frames(sock, 2, timeout=1)
        for frame in frames:
            self.assertIsNot(frame.header.get("ok"), True, frame.header)
        self.assertEqual(self.controller.control_calls(), [])
        # an UNattached-but-not-closed variant: the name of the first hello still governs
        sock = self.raw_hello("workbench-ui")
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p3"}))
        reply, = self.frames(sock)
        self.assert_refused(reply.header)
        self.assertEqual(self.controller.control_calls(), [])

    def test_identity_claims_in_request_headers_are_ignored(self):
        sock = self.raw_hello("workbench-ui")
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p3", "client": HUB, "uid": EUID,
                                         "peer_uid": EUID, "attached": True, "reconciled": True}))
        reply, = self.frames(sock)
        self.assert_refused(reply.header)
        self.assertEqual(self.controller.control_calls(), [])

    def test_hub_name_unlocks_nothing_that_needs_attach(self):
        hub = self.client(HUB)
        for kind, fields in ATTACH_REQUIRED.items():
            with self.subTest(kind=kind.value):
                got = hub.request(kind, _PAYLOAD.get(kind, b""), **fields)
                self.assert_refused(got)
        self.assertEqual(self.controller.control_calls(), [])
        self.assertIsNone(self.attached())

    def test_important_log_mutations_stay_closed_to_hub(self):
        hub = self.client(HUB)
        archive = hub.request(ClientType.IMPORTANT_ARCHIVE_CLOSE, subject_kind="task", subject_id=STOP_ID,
                              confirmed=True)
        delete = hub.request(ClientType.IMPORTANT_DELETE_DB, db_uuid=STOP_ID, confirmed=True)
        self.assertFalse(archive["ok"])
        self.assertFalse(delete["ok"])
        self.assertEqual(self.controller.control_calls(), [])

    def test_hub_name_does_not_unlock_attach_requiring_after_pause(self):
        hub = self.client(HUB)
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assert_refused(hub.request(ClientType.INPUT, b"x", pane=PaneId.HOST_SHELL.value))
        self.assert_refused(hub.request(ClientType.DETACH))
        self.assertEqual(self.controller.control_calls(), ["pause"])

    def test_previously_attached_then_detached_connection_gets_no_special_standing(self):
        ui = self.attach_ui("workbench-ui")
        self.assertTrue(ui.detach()["ok"])
        self.assert_refused(ui.request(ClientType.PAUSE))
        self.assert_refused(ui.request(ClientType.RESUME, reconciled=True))
        self.assertEqual(self.controller.control_calls(), [])

    def test_existing_nonattached_requests_are_unchanged(self):
        for name in ("workbench-ui", HUB):
            with self.subTest(name=name):
                client = self.client(name)
                self.assertIn("focus", client.snapshot())
                self.assertIn("token", client.request(ClientType.SHUTDOWN_REQUEST))
                self.assertTrue(client.request(ClientType.SHUTDOWN_CONFIRM, token="tok")["ok"])
                self.assertFalse(client.request(ClientType.CONFIRM_BOOT, boot_id="b")["ok"])
                self.controller.shutdown_token = None
                self.controller.shutdown_confirmed = False

    # ---- protocol edge: before hello / wrong version ---------------------------
    def test_pause_before_hello_is_rejected_and_closed(self):
        sock = self.raw()
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p"}))
        reject, = self.frames(sock)
        self.assertEqual(reject.header["reason"], "hello_required")
        self.assertEqual(sock.recv(10), b"")
        self.assertEqual(self.controller.control_calls(), [])

    def test_pause_pipelined_before_hello_cannot_inherit_a_hub_name(self):
        sock = self.raw()
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p", "client": HUB})
                     + ui_v1.encode_frame(ui_v1.hello(HUB)))
        frames = self.frames(sock)
        self.assertEqual(frames[0].header["reason"], "hello_required")
        self.assertEqual(self.controller.control_calls(), [])

    def test_hello_version_mismatch_rejects_and_hub_name_does_not_help(self):
        sock = self.raw()
        sock.sendall(ui_v1.encode_frame(ui_v1.hello(HUB, versions=(2,))) +
                     ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p"}))
        frames = self.frames(sock, 1)
        self.assertEqual(frames[0].header["reason"], "version_mismatch")
        self.assertEqual(sock.recv(100) if len(frames) < 2 else b"", b"")
        self.assertEqual(self.controller.control_calls(), [])
        with self.assertRaises(ClientError):
            UiClient(self.path, timeout=5, name=HUB, versions=(2,))
        self.assertEqual(self.controller.control_calls(), [])

    def test_wrong_frame_version_after_hello_is_rejected(self):
        for bad in (2, 0, "1", True, None, 1.0):
            with self.subTest(v=bad):
                sock = self.raw_hello(HUB)
                header = {"type": "pause", "id": "p"}
                if bad is not None:
                    header["v"] = bad
                sock.sendall(ui_v1.encode_frame(header))
                reply, = self.frames(sock)
                self.assertEqual(reply.header["reason"], "version_mismatch", reply.header)
                self.assertEqual(sock.recv(10), b"")
        self.assertEqual(self.controller.control_calls(), [])

    def test_pause_with_payload_is_invalid(self):
        sock = self.raw_hello(HUB)
        sock.sendall(ui_v1.encode_frame({"v": 1, "type": "pause", "id": "p"}, b"junk"))
        reply, = self.frames(sock)
        self.assertFalse(reply.header["ok"])
        self.assertEqual(self.controller.control_calls(), [])

    # ---- peer uid -----------------------------------------------------------
    def test_peer_identity_reports_the_real_uid_by_default(self):
        hub = self.client(HUB)
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        client_sock = self.raw()
        with self.lock:
            pid, uid = self.server.peer_identity(client_sock)  # a connected socket, not an accepted one
        self.assertIsInstance(pid, int)
        self.assertEqual(uid, EUID)


class ForeignPeerTests(Fixture):
    server_class = ForeignUidServer

    def test_peer_uid_mismatch_is_not_attached(self):
        for uid in (EUID + 1, 0 if EUID != 0 else 1, 2 ** 32 - 1):
            with self.subTest(uid=uid):
                ForeignUidServer.uid = uid
                hub = self.client(HUB)
                self.assert_refused(hub.request(ClientType.PAUSE))
                self.assert_refused(hub.request(ClientType.RESUME, reconciled=True))
        ForeignUidServer.uid = EUID + 1
        self.assertEqual(self.controller.control_calls(), [])
        self.assertFalse(self.controller.paused)

    def test_attached_connection_is_unaffected_by_peer_uid(self):
        ui = self.attach_ui()
        self.assertTrue(ui.request(ClientType.PAUSE)["ok"])
        self.assertTrue(ui.request(ClientType.RESUME, reconciled=True)["ok"])
        self.assertEqual(self.controller.control_calls(), ["pause", "resume"])

    def test_foreign_hub_does_not_disturb_attached_ui(self):
        ui = self.attach_ui()
        hub = self.client(HUB)
        self.assert_refused(hub.request(ClientType.PAUSE))
        self.settle(ui)
        self.assertEqual(ui.states, [])
        self.assertIs(self.attached() is not None, True)
        self.assertTrue(ui.request(ClientType.FOCUS, pane=PaneId.MANAGER_OMP.value)["ok"])

    def test_foreign_uid_leaves_snapshot_and_shutdown_unchanged(self):
        hub = self.client(HUB)
        self.assertIn("focus", hub.snapshot())
        self.assertIn("token", hub.request(ClientType.SHUTDOWN_REQUEST))


class UnreadablePeerTests(Fixture):
    server_class = UnreadablePeerServer

    def test_unreadable_peer_is_not_attached_and_server_keeps_running(self):
        hub = self.client(HUB)
        self.assert_refused(hub.request(ClientType.PAUSE))
        self.assert_refused(hub.request(ClientType.RESUME, reconciled=True))
        self.assertIn("focus", hub.snapshot())
        self.assertEqual(self.controller.control_calls(), [])

    def test_unreadable_peer_can_still_attach_and_use_pause_as_attached(self):
        ui = self.attach_ui(HUB)
        self.assertTrue(ui.request(ClientType.PAUSE)["ok"])


class PerConnectionPeerTests(Fixture):
    server_class = FirstForeignServer

    def setUp(self):
        FirstForeignServer.seen = 0
        super().setUp()

    def test_peer_is_recorded_per_connection(self):
        foreign = self.client(HUB)  # first accepted connection: foreign uid
        good = self.client(HUB)
        self.assert_refused(foreign.request(ClientType.PAUSE))
        self.assertEqual(self.controller.control_calls(), [])
        self.assertTrue(good.request(ClientType.PAUSE)["ok"])
        self.assertEqual(self.controller.control_calls(), ["pause"])
        # the foreign connection's standing does not change after a good one succeeded
        self.assert_refused(foreign.request(ClientType.RESUME, reconciled=True))
        self.assertEqual(self.controller.control_calls(), ["pause"])


class AttachedUiCoexistenceTests(Fixture):
    def wait_pushes(self, ui, count, timeout=5):
        deadline = time.monotonic() + timeout
        while len(ui.states) < count and time.monotonic() < deadline:
            ui.pump(0.05)
        self.assertGreaterEqual(len(ui.states), count)

    def test_hub_pause_keeps_ui_attached_and_pushes_state(self):
        ui = self.attach_ui()
        attached_before = self.attached()
        stats = dict(self.server.stats)
        hub = self.client(HUB)
        result = hub.request(ClientType.PAUSE)
        self.assertTrue(result["ok"])
        self.assertEqual(self.controller.control_calls(), ["pause"])
        self.assertIs(self.attached(), attached_before)
        self.assertEqual(self.controller.calls.count("on_attach"), 1)
        self.assertEqual(self.controller.calls.count("on_detach"), 0)
        with self.lock:
            self.assertEqual(self.server.stats["attaches"], stats["attaches"])
            self.assertEqual(self.server.stats["detaches"], stats["detaches"])
        self.wait_pushes(ui, 1)
        self.assertTrue(ui.states[-1]["paused"])
        # the requester gets no state push
        self.settle(hub)
        self.assertEqual(hub.states, [])
        # the UI keeps working as the attached client
        self.assertTrue(ui.input(PaneId.HOST_SHELL, b"hi")["ok"])

    def test_hub_resume_pushes_state_to_ui(self):
        ui = self.attach_ui()
        hub = self.client(HUB)
        hub.request(ClientType.PAUSE)
        self.assertTrue(hub.request(ClientType.RESUME, reconciled=True)["ok"])
        self.wait_pushes(ui, 2)
        self.assertEqual([s["paused"] for s in ui.states], [True, False])
        self.assertIsNotNone(self.attached())

    def test_refused_requests_push_nothing(self):
        ui = self.attach_ui()
        for name in ("workbench-cli", "workbench-ui", "Workbench-Hub"):
            self.assert_refused(self.client(name).request(ClientType.PAUSE))
        self.assert_refused(self.client(HUB).request(ClientType.RESUME, reconciled=False), "resume_not_reconciled")
        self.settle(ui)
        self.assertEqual(ui.states, [])
        # only the reconciled:false resume reached the controller (it owns that rule); the others never did
        self.assertEqual(self.controller.control_calls(), ["resume"])
        self.assertFalse(self.controller.paused)

    def test_controller_failure_for_hub_is_an_error_not_a_push_or_crash(self):
        ui = self.attach_ui()
        hub = self.client(HUB)
        self.controller.fail_pause = True
        result = hub.request(ClientType.PAUSE)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "internal_error")
        self.settle(ui)
        self.assertEqual(ui.states, [])
        self.controller.fail_pause = False
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertIsNotNone(self.attached())

    def test_hub_closing_does_not_detach_ui(self):
        ui = self.attach_ui()
        hub = self.client(HUB)
        hub.request(ClientType.PAUSE)
        hub.close()
        time.sleep(0.1)
        self.assertEqual(self.controller.calls.count("on_detach"), 0)
        self.assertIsNotNone(self.attached())
        self.assertTrue(ui.input(PaneId.HOST_SHELL, b"hi")["ok"])

    def test_hub_attach_while_ui_attached_is_attached_elsewhere(self):
        ui = self.attach_ui()
        hub = self.client(HUB)
        self.assert_refused(hub.attach(), "attached_elsewhere")
        # the failed attach changes nothing: the UI is still the attached one and the hub's pause is still non-attached
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assert_refused(hub.request(ClientType.INPUT, b"x", pane=PaneId.HOST_SHELL.value))
        self.wait_pushes(ui, 1)
        self.assertEqual(self.controller.calls.count("on_attach"), 1)
        self.assertTrue(ui.input(PaneId.HOST_SHELL, b"hi")["ok"])

    def test_hub_pause_does_not_consume_the_attach_slot(self):
        hub = self.client(HUB)
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertTrue(hub.request(ClientType.RESUME, reconciled=True)["ok"])
        ui = self.client("workbench-ui")
        self.assertTrue(ui.attach()["ok"])
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertTrue(ui.input(PaneId.HOST_SHELL, b"x")["ok"])
        other = self.client("workbench-ui")
        self.assert_refused(other.attach(), "attached_elsewhere")

    def test_hub_attached_itself_uses_normal_attached_path(self):
        hub = self.attach_ui(HUB)
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.wait_pushes(hub, 1)  # an attached requester does get the push, as before
        other = self.client("workbench-ui")
        self.assert_refused(other.attach(), "attached_elsewhere")

    def test_hub_request_right_after_attached_ui_disappears(self):
        ui = self.attach_ui()
        hub = self.client(HUB)
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        ui.sock.close()  # abrupt disconnect of the attached UI
        self.wait_for(lambda: self.server.attached is None, "UI detach after disconnect")
        self.assertEqual(self.controller.calls.count("on_detach"), 1)
        self.assertTrue(hub.request(ClientType.RESUME, reconciled=True)["ok"])  # nobody to push to: no crash
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertEqual(self.controller.control_calls(), ["pause", "resume", "pause"])
        self.assert_refused(hub.request(ClientType.INPUT, b"x", pane=PaneId.HOST_SHELL.value))
        fresh = self.client("workbench-ui")
        self.assertTrue(fresh.attach()["ok"])

    def test_detached_ui_connection_loses_pause_but_hub_keeps_it(self):
        ui = self.attach_ui("workbench-ui")
        hub = self.client(HUB)
        self.assertTrue(ui.detach()["ok"])
        self.assert_refused(ui.request(ClientType.PAUSE))
        self.assertTrue(hub.request(ClientType.PAUSE)["ok"])
        self.assertEqual(self.controller.control_calls(), ["pause"])

    def test_interleaved_ui_pause_and_hub_resume_are_serialized(self):
        rounds = 40
        ui = self.attach_ui()
        hub = self.client(HUB)
        barrier = threading.Barrier(2)
        results: dict[str, list] = {"ui": [], "hub": []}
        errors: list[BaseException] = []

        def run(label, client, kind, fields):
            try:
                barrier.wait(5)
                for _ in range(rounds):
                    results[label].append(client.request(kind, **fields))
            except BaseException as exc:  # reported in the main thread
                errors.append(exc)

        threads = [threading.Thread(target=run, args=("ui", ui, ClientType.PAUSE, {})),
                   threading.Thread(target=run, args=("hub", hub, ClientType.RESUME, {"reconciled": True}))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results["ui"]), rounds)
        self.assertEqual(len(results["hub"]), rounds)
        self.assertTrue(all(r["ok"] for r in results["ui"] + results["hub"]))
        log = self.controller.control_calls()
        self.assertEqual(sorted(log), ["pause"] * rounds + ["resume"] * rounds)
        # the UI's push stream is the state after each call, in the controller's call order, with nothing missing
        self.wait_pushes(ui, 2 * rounds)
        self.assertEqual([s["paused"] for s in ui.states], [call == "pause" for call in log])
        self.assertEqual(self.controller.paused, log[-1] == "pause")
        self.assertIsNotNone(self.attached())
        self.assertEqual(self.controller.calls.count("on_detach"), 0)
        # the hub (non-attached) connection got no pushes
        self.settle(hub)
        self.assertEqual(hub.states, [])


if __name__ == "__main__":
    unittest.main()
