"""Handwritten independent records-v1 Python/TypeScript acceptance cases."""
import json
from pathlib import Path
import subprocess
import unittest

from workbench.contracts.records_v1 import RecordContractError, parse_request

ROOT = Path(__file__).resolve().parents[2]
ID = "9bb43b2e-e51f-4e4b-8246-7892c6e739f1"


def request(action, **values):
    # Explicit wire names from SPEC, never production REQUEST_FIELDS/action helpers.
    value = dict(action=action, record_id=None, revision=None, expected_revision=None,
                 title=None, body=None, task_id=None, query=None, limit=None,
                 after=None, output_path=None)
    value.update(values)
    return value


class IndependentContractTests(unittest.TestCase):
    def test_python_and_typescript_preserve_independent_requests_and_reject_bad_boundaries(self):
        valid = [
            request("create", title="😀" * 256, body="한" * 21845 + "x"),
            request("revise", record_id=ID, expected_revision=1, title="e\u0301", body="\r\n ", task_id="task-a"),
            request("show", record_id=ID, revision=2),
            request("history", record_id=ID, limit=100, after="opaque-cursor"),
            request("list", task_id="task-a", limit=1),
            request("search", query="😀" * 256),
            request("recent"),
            request("export", record_id=ID),  # Host must report needs_output_path, parser must retain null.
            request("export", record_id=ID, revision=1, output_path="결과/메모.md"),
        ]
        invalid = [
            request("create", title="😀" * 257, body="x"),
            request("create", title="x", body="한" * 21845 + "xy"),
            request("search", query="😀" * 257),
            request("revise", record_id=ID, expected_revision=True, title="x", body="y"),
            request("show", record_id=ID, revision=1.5),
            request("recent", after="cursor"),
            request("create", title="x", body="y", actor="manager"),
            request("create", title="x", body="y", data_root="/tmp/other"),
            request("export", record_id=ID, output_path="bad\x00path"),
        ]
        missing = request("list")
        del missing["revision"]
        invalid.append(missing)
        for value in valid:
            with self.subTest(action=value["action"]):
                self.assertEqual(parse_request(value), value)
        for index, value in enumerate(invalid):
            with self.subTest(invalid_index=index), self.assertRaises(RecordContractError):
                parse_request(value)
        # The JavaScript process consumes the exact independently authored JSON input;
        # successful values must round-trip, beyond merely returning true.
        program = """import fs from 'node:fs';
import {parseRequest} from './omp_bridge/contract/records_v1.ts';
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(value=>{
 try{return {ok:true,value:parseRequest(value)}}catch(error){return {ok:false}}
})));"""
        result = subprocess.run(["node", "--experimental-strip-types", "--no-warnings",
                                 "--input-type=module", "-e", program], cwd=ROOT,
                                input=json.dumps(valid + invalid), text=True,
                                capture_output=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout),
                         [{"ok": True, "value": value} for value in valid] +
                         [{"ok": False} for _ in invalid])
