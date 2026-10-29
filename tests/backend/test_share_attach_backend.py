"""WBX-E1 (10)-(12): the Hub's shared attach against a real ``Backend`` (fake OMPs, real host shell PTY).

Seam: the real ``Backend`` fixture of the command-stop tests (``BackendStopFixture``: fake OMPs, a real host shell, the
backend loop run by this thread) with real ``UiClient`` connections on the backend's ui socket. This test process
plays the verified Hub: the backend's ``hub_peer`` check is replaced by one that accepts this process's pid only.

Expected values are this file's own, from web-board-ux SPEC s5 "ui_v1 덧붙이기: 공유 attach": the snapshot carries
``shared_attachments`` (``attached`` keeps meaning the primary); the periodic state push reaches a shared attachment
with no primary; a shared attachment's input goes through the same admit path with the same refusal reasons and the
same lone Ctrl+C exception and ``command_interrupt`` record (``requested_by: user``); the host shell input owner is one
per backend, changed alike from the primary or a shared attachment and pushed to every attachment.
"""

from __future__ import annotations

import os
import threading

from test_command_stop_authority import BackendStopFixture
from workbench.backend.client import UiClient
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId

HUB = "workbench-hub"
UI = "workbench-ui"


class SharedBackendFixture(BackendStopFixture):
    def setUp(self):
        super().setUp()
        self.backend.ui.hub_peer = lambda pid: pid == os.getpid()

    def off_loop(self, call, what):
        """Run a blocking client call on another thread while this thread runs the backend loop."""
        box: dict = {}

        def run():
            try:
                box["value"] = call()
            except BaseException as exc:  # handed back to the test
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

    def shared(self):
        hub = self.connect(HUB)
        result = self.request(hub, ClientType.SHARE_ATTACH)
        self.assertTrue(result["ok"], result)
        return hub, result

    def tui(self):
        ui = self.connect(UI)
        self.assertTrue(self.request(ui, ClientType.ATTACH, size={"rows": 30, "cols": 100})["ok"])
        return ui

    def wait_state(self, client, predicate, what):
        def seen():
            client.pump(0)
            return any(predicate(state) for state in client.states)
        self.wait(seen, what)


class SnapshotAndStatePushTests(SharedBackendFixture):
    """(10) ``shared_attachments`` in the snapshot; the periodic push reaches a shared attachment alone."""

    def test_the_snapshot_counts_shared_attachments_and_attached_keeps_meaning_the_primary(self):
        before = self.backend.snapshot()
        self.assertEqual((before["attached"], before["shared_attachments"]), (False, 0))
        hub, result = self.shared()
        self.assertEqual((result["snapshot"]["attached"], result["snapshot"]["shared_attachments"]), (False, 1))
        self.shared()
        self.assertEqual(self.backend.snapshot()["shared_attachments"], 2)
        self.tui()
        now = self.backend.snapshot()
        self.assertEqual((now["attached"], now["shared_attachments"]), (True, 2))
        self.assertTrue(self.request(hub, ClientType.SHARE_DETACH)["detached"])
        self.assertEqual(self.backend.snapshot()["shared_attachments"], 1)

    def test_the_periodic_state_push_reaches_a_shared_attachment_without_a_primary(self):
        hub, _ = self.shared()
        self.wait_state(hub, lambda state: state.get("shared_attachments") == 1, "a pushed state")
        self.assertIsNone(self.backend.ui.attached)


class SharedInputTests(SharedBackendFixture):
    """(11) Shared input takes the primary's admit path: the same refusals, the lone Ctrl+C and its record."""

    def test_host_refusals_match_the_primary_and_a_lone_ctrl_c_is_forwarded_and_journaled(self):
        ui, (hub, _) = self.tui(), self.shared()
        self.open_free_task()
        started = self.run_command("sleep 614.2")
        self.wait(lambda: self.backend.snapshot()["panes"]["host_shell"]["input_owner"] == "manager",
                  "the Worker's command holding the shell")
        refusals = [self.request(client, ClientType.INPUT, b"echo no\r", pane="host_shell") for client in (hub, ui)]
        self.assertEqual([(r["ok"], r.get("reason")) for r in refusals], [(False, "input_owner_manager")] * 2)
        self.assertEqual(self.records("command_interrupt"), [])
        forwarded = self.request(hub, ClientType.INPUT, b"\x03", pane="host_shell")
        self.assertEqual((forwarded["ok"], forwarded["accepted_bytes"]), (True, 1), forwarded)
        self.wait(lambda: bool(self.records("command_interrupt")), "the command_interrupt record")
        (record,) = self.records("command_interrupt")
        self.assertEqual((record["requested_by"], record["command_id"]), ("user", started["command_id"]), record)
        self.assertNotIn("client", record, "the journal does not name the client")

    def test_omp_input_is_accepted_and_host_kill_restart_answer_like_the_primary(self):
        ui, (hub, _) = self.tui(), self.shared()
        accepted = self.request(hub, ClientType.INPUT, b"hello", pane="worker_omp")
        self.assertEqual((accepted["ok"], accepted["pane"], accepted["accepted_bytes"]), (True, "worker_omp", 5))
        for pane in ("manager_omp", "worker_omp"):
            refused = self.request(hub, ClientType.KILL_PANE, pane=pane)
            self.assertEqual((refused["ok"], refused.get("reason")), (False, "pane_not_killable"), refused)
        killed = self.request(hub, ClientType.KILL_PANE, pane="host_shell")
        self.assertEqual((killed["ok"], killed["killed"]), (True, True), killed)
        self.wait_state(ui, lambda s: s["panes"]["host_shell"]["alive"] is False, "the UI seeing the kill")
        refusals = [self.request(client, ClientType.INPUT, b"x", pane="host_shell") for client in (hub, ui)]
        self.assertEqual(refusals[0].get("reason"), refusals[1].get("reason"))
        self.assertFalse(refusals[0]["ok"])
        restarted = self.request(hub, ClientType.RESTART_PANE, pane="host_shell")
        self.assertTrue(restarted["ok"], restarted)
        self.wait_state(ui, lambda s: s["panes"]["host_shell"]["alive"] is True, "the UI seeing the restart")


class OneInputOwnerTests(SharedBackendFixture):
    """(12) One host shell input owner per backend: either side's handoff/takeover changes it for both."""

    def owner(self):
        return self.backend.snapshot()["panes"]["host_shell"]["input_owner"]

    def test_a_shared_handoff_and_a_primary_takeover_change_the_one_owner_seen_by_every_attachment(self):
        ui, (hub, _) = self.tui(), self.shared()
        self.wait(lambda: self.backend.snapshot()["panes"]["host_shell"]["shell"]["parent_mode"] == "manual_prompt",
                  "the shell prompt")
        self.assertTrue(self.request(hub, ClientType.INPUT, b"wb-handoff\r", pane="host_shell")["ok"])
        self.wait(lambda: self.backend.snapshot()["panes"]["host_shell"]["shell"]["parent_mode"] == "control_wait",
                  "the shell waiting for control")
        handed = self.request(hub, ClientType.HANDOFF)
        self.assertEqual((handed["ok"], handed["shell"]["input_owner"]), (True, "manager"), handed)
        for client in (ui, hub):
            self.wait_state(client, lambda s: s["panes"]["host_shell"]["input_owner"] == "manager", "manager owner")
        held = self.request(ui, ClientType.INPUT, b"echo no\r", pane="host_shell")
        self.assertEqual((held["ok"], held.get("reason")), (False, "input_owner_manager"), held)
        taken = self.request(ui, ClientType.TAKEOVER_REQUEST)
        self.assertEqual((taken["ok"], taken["shell"]["input_owner"]), (True, "user"), taken)
        for client in (ui, hub):
            client.states.clear()
            self.wait_state(client, lambda s: s["panes"]["host_shell"]["input_owner"] == "user", "user owner")
        self.wait(lambda: self.backend.snapshot()["panes"]["host_shell"]["shell"]["parent_mode"] == "manual_prompt",
                  "the prompt after the takeover")
        self.assertTrue(self.request(hub, ClientType.INPUT, b"true\r", pane="host_shell")["ok"])
        self.assertEqual(self.owner(), "user")


class _Sender:
    def __init__(self):
        self.sent = []

    def send(self, kind, payload=b"", **fields):
        self.sent.append((kind.value, dict(fields), payload))
        return f"r{len(self.sent)}"


class TuiIgnoresTheNewKeyTests(SharedBackendFixture):
    """(13) The product UI model treats a snapshot with ``shared_attachments`` exactly like one without it."""

    @staticmethod
    def seen(snapshots):
        from workbench.ui.product.model import ProductModel
        sender = _Sender()
        model = ProductModel(sender, 30, 120, clock=lambda: 1000.0, monotonic=lambda: 5.0, environ={})
        model.attach_done(snapshots[0])
        for snapshot in snapshots[1:]:
            model.on_state(snapshot)
        return (model.status_lines(), model.footer(), model.menu_lines(), model.focus, model.input_owner(),
                model.shown_owner(), model.take_output(), sender.sent)

    def test_the_ui_model_ignores_shared_attachments(self):
        ui = self.tui()
        hub, result = self.shared()
        with_key = [self.request(ui, ClientType.SNAPSHOT)["snapshot"], result["snapshot"]]
        self.assertEqual([s["shared_attachments"] for s in with_key], [1, 1])
        without_key = [{k: v for k, v in s.items() if k != "shared_attachments"} for s in with_key]
        self.assertEqual(self.seen(with_key), self.seen(without_key))
