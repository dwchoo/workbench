"""CS-O10 independent protocol checks: the ``stop_command`` client message and its ``final`` result frame.

Derived from the task packet and FOLLOWUPS-BRIEF FU-D06/FU-D08: ``command_id`` is required and a canonical UUID,
the message carries no payload, no new client message type sneaks in beside it, and a final stop result is an
ordinary ``result`` frame with ``final: true`` that encodes like any other frame.
"""
import unittest
from uuid import uuid4

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, ContractError, Frame, Reason, ServerType

BASELINE_CLIENT_TYPES = {
    "hello", "attach", "detach", "snapshot", "input", "paste", "resize", "focus", "takeover_request",
    "takeover_confirm", "handoff", "shutdown_request", "shutdown_confirm", "confirm_boot", "restart_pane",
    "kill_pane", "pause", "resume", "important_archive_close", "important_delete_db", "raw_query", "raw_status"}


def parse(header, payload=b"", discarded=0, version=1):
    return ui_v1.parse_client_frame(Frame(header, payload, discarded), version)


class StopCommandParseTests(unittest.TestCase):
    def test_exactly_the_known_new_client_types_exist(self):
        # stop_command (CS) plus the three Hub-only Task Inbox requests (TI-09, SPEC "Hub -> backend") and the two
        # Hub-only shared attach requests (WBX-E1, web-board-ux SPEC s5); nothing else.
        self.assertEqual({t.value for t in ClientType},
                         BASELINE_CLIENT_TYPES | {"stop_command", "inbox_notice", "task_status", "cancel_task",
                                                  "share_attach", "share_detach"})

    def test_a_canonical_uuid_of_any_version_parses_to_that_id_only(self):
        ids = [str(uuid4()), "12345678-1234-1234-8234-123456789abc", "01890a5d-ac96-774b-bcce-b302099a8057",
               "5f1c2a9e-3b4d-4e6f-8a7b-0c1d2e3f4a5b"]
        for command_id in ids:
            with self.subTest(command_id=command_id):
                message = parse({"v": 1, "type": "stop_command", "id": "r1", "command_id": command_id})
                self.assertEqual((message.type, message.id, message.fields, message.payload),
                                 (ClientType.STOP_COMMAND, "r1", {"command_id": command_id}, b""))

    def test_everything_else_is_a_contract_error(self):
        good = str(uuid4())
        bad = [None, "", " ", "x", good.upper(), good.replace("-", ""), f" {good}", f"{good} ", f"{good}\n",
               f"urn:uuid:{good}", f"{{{good}}}", good[:-1], good + "0", 7, 7.5, True, [good], {"id": good},
               b"x", "١" * 36]
        for value in bad:
            with self.subTest(command_id=value):
                with self.assertRaises(ContractError):
                    parse({"v": 1, "type": "stop_command", "id": "r1", "command_id": value})
        with self.assertRaises(ContractError):
            parse({"v": 1, "type": "stop_command", "id": "r1"})

    def test_a_payload_or_discarded_bytes_are_refused(self):
        header = {"v": 1, "type": "stop_command", "id": "r1", "command_id": str(uuid4())}
        with self.assertRaises(ContractError):
            parse(header, payload=b"x")
        with self.assertRaises(ContractError):
            parse(header, discarded=5)

    def test_the_request_id_and_version_rules_apply(self):
        command_id = str(uuid4())
        for header in ({"v": 1, "type": "stop_command", "command_id": command_id},
                       {"v": 1, "type": "stop_command", "id": "", "command_id": command_id},
                       {"v": 1, "type": "stop_command", "id": "x" * 65, "command_id": command_id},
                       {"v": 2, "type": "stop_command", "id": "a", "command_id": command_id},
                       {"type": "stop_command", "id": "a", "command_id": command_id}):
            with self.subTest(header=header):
                with self.assertRaises(ContractError):
                    parse(header)
        with self.assertRaises(ContractError):
            parse({"v": 1, "type": "stop_command", "id": "a", "command_id": command_id}, version=None)

    def test_near_miss_type_names_are_unsupported(self):
        for name in ("stop", "Stop_Command", "stop-command", "stop_commands", "stop_command ", "kill_command"):
            with self.subTest(name=name):
                with self.assertRaises(ContractError):
                    parse({"v": 1, "type": name, "id": "a", "command_id": str(uuid4())})


class FinalFrameTests(unittest.TestCase):
    def test_a_final_result_is_an_ordinary_result_frame_that_round_trips(self):
        command_id = str(uuid4())
        stop = {"status": "exited", "exit_code": 130, "signal": 2, "signal_name": "SIGINT", "command_id": command_id,
                "signals": [{"signal": "SIGINT", "at": 1.5, "targets": [{"pid": 1, "start_ticks": 2}]}]}
        header = ui_v1.result("r1", True, final=True, command_id=command_id, stop=stop)
        self.assertEqual((header["type"], header["id"], header["ok"], header["final"]),
                         (ServerType.RESULT.value, "r1", True, True))
        decoder = ui_v1.FrameDecoder()
        (frame,) = decoder.feed(ui_v1.encode_frame(header))
        self.assertEqual(frame.header, header)

    def test_the_ack_and_the_final_differ_only_by_final_and_stop(self):
        command_id = str(uuid4())
        ack = ui_v1.result("r1", True, command_id=command_id, stop={"status": "stopping"})
        final = ui_v1.result("r1", True, final=True, command_id=command_id, stop={"status": "exited"})
        self.assertNotIn("final", ack)
        self.assertEqual({k for k in final if k not in ack or final[k] != ack[k]}, {"final", "stop"})

    def test_queue_full_is_a_contract_reason(self):
        self.assertIn("queue_full", {r.value for r in Reason})
