"""Hand-authored s1 wire fixtures; Python and TypeScript must accept the same values."""
import json
from pathlib import Path
import unittest

from workbench.contracts.records_v1 import parse_request, parse_record

FIXTURE = Path(__file__).parent / "fixtures/work-records-v1.json"


class RecordsContractTests(unittest.TestCase):
    def test_handwritten_requests_and_snapshot_roundtrip(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual([parse_request(value) for value in fixture["requests"]], fixture["requests"])
        self.assertEqual(parse_record(fixture["record"]), fixture["record"])

    def test_action_fields_and_unicode_limits_are_strict_in_both_languages(self):
        from workbench.contracts.records_v1 import RecordContractError
        import subprocess
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        create = fixture["requests"][0]
        good = [{**create, "title": "😀" * 256, "body": "가" * 21845 + "a"}]
        bad = [
            {**create, "unexpected": 1}, {**create, "body": ""},
            {**create, "body": "가" * 21845 + "aa"},
            {**create, "title": "😀" * 257}, {**create, "title": ""},
            {**create, "body": "\ud800"}, {**create, "actor": "manager"},
            {**create, "record_id": "11111111-1111-4111-8111-111111111111"},
            {**fixture["requests"][1], "expected_revision": True},
            {**fixture["requests"][2], "record_id": "../elsewhere"},
            {**fixture["requests"][2], "revision": 0},
            {**fixture["requests"][4], "limit": 101},
            {**fixture["requests"][5], "query": ""},
            {**fixture["requests"][6], "limit": 1},
            {**fixture["requests"][7], "output_path": "bad\u0000path"},
        ]
        for value in good:
            self.assertEqual(parse_request(value), value)
        for value in bad:
            with self.subTest(value=str(value)[:80]), self.assertRaises(RecordContractError):
                parse_request(value)
        program = """import fs from 'node:fs'; import {parseRequest} from './omp_bridge/contract/records_v1.ts';
const cases=JSON.parse(fs.readFileSync(0,'utf8')); process.stdout.write(JSON.stringify(cases.map(x=>{try{parseRequest(x);return true}catch(e){return false}})));"""
        node = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", program],
            input=json.dumps(good + bad), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(node.stdout), [True] + [False] * len(bad))

    def test_stored_snapshot_rejects_invalid_identity_revision_role_and_time(self):
        from workbench.contracts.records_v1 import RecordContractError
        record = json.loads(FIXTURE.read_text(encoding="utf-8"))["record"]
        for changed in ({"revision": True}, {"actor": "model-supplied"}, {"record_id": "bad"},
                        {"recorded_at": "not-a-time"}, {"created_at": "2026-02-30T00:00:00Z"},
                        {"body": "\ud800"}, {"extra": 1}):
            with self.subTest(changed=changed), self.assertRaises(RecordContractError):
                parse_record({**record, **changed})
