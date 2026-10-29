"""WBX-E1 (1): ui_v1 additive Hub shared attach: ``share_attach``/``share_detach`` and ``resize_owned_by_ui``.

Seam: the contract seam (``parse_client_frame`` and the enums). Expected values are the web-board-ux SPEC s5 table
"ui_v1 덧붙이기: 공유 attach": two request types without fields (any other field is refused), one reason, the version
stays 1.
"""

from __future__ import annotations

import unittest

from workbench.contracts import ui_v1
from workbench.contracts.ui_v1 import ClientType, Frame, Reason
from workbench.contracts.v1 import ContractError


def parse(header, payload=b""):
    return ui_v1.parse_client_frame(Frame(header, payload), 1)


class ShareAttachContractTests(unittest.TestCase):
    def test_share_attach_and_share_detach_parse_with_only_v_type_and_id(self):
        for value, kind in (("share_attach", ClientType.SHARE_ATTACH), ("share_detach", ClientType.SHARE_DETACH)):
            with self.subTest(kind=value):
                message = parse({"v": 1, "type": value, "id": "s1"})
                self.assertEqual((message.type, message.id, message.fields, message.payload), (kind, "s1", {}, b""))

    def test_any_other_field_or_a_payload_is_a_contract_error(self):
        extras = ({"size": {"rows": 24, "cols": 80}}, {"pane": "host_shell"}, {"client": "x"}, {"x": None})
        for value in ("share_attach", "share_detach"):
            for extra in extras:
                with self.subTest(kind=value, extra=extra), self.assertRaises(ContractError):
                    parse({"v": 1, "type": value, "id": "s1", **extra})
            with self.subTest(kind=value, payload=True), self.assertRaises(ContractError):
                parse({"v": 1, "type": value, "id": "s1"}, b"x")
            with self.subTest(kind=value, id=None), self.assertRaises(ContractError):
                parse({"v": 1, "type": value})

    def test_resize_owned_by_ui_is_a_reason_and_the_version_stays_1(self):
        self.assertEqual(Reason.RESIZE_OWNED_BY_UI.value, "resize_owned_by_ui")
        self.assertEqual((ui_v1.VERSION, ui_v1.SUPPORTED_VERSIONS), (1, (1,)))


if __name__ == "__main__":
    unittest.main()
