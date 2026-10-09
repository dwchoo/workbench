"""Independent behavior tests for the frozen Python/TypeScript log JSON v1 wire."""
from __future__ import annotations

import copy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import unittest

from workbench.contracts.logs_v1 import (
    LogContractError, canonical_json, decode_json, parse_source_range,
    parse_observation_boundary, parse_observation_coverage,
    parse_source_ref, parse_summary_request, parse_summary_result, parse_subject_ref,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/contracts/fixtures/logs-summary-v1.json"


class IndependentLogWireContract(unittest.TestCase):
    def setUp(self) -> None:
        self.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def node(self, program: str, payload: object) -> object:
        process = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", program],
            input=json.dumps(payload, ensure_ascii=True), text=True, capture_output=True,
            cwd=ROOT, timeout=15, check=True,
        )
        return json.loads(process.stdout)

    def test_golden_request_result_have_exact_cross_language_bytes(self) -> None:
        expected = self.golden["canonicalSha256"]
        self.assertEqual(sha256(canonical_json(parse_summary_request(self.golden["request"]))).hexdigest(), expected["request"])
        self.assertEqual(sha256(canonical_json(parse_summary_result(self.golden["result"]))).hexdigest(), expected["result"])
        program = """import fs from 'node:fs'; import {createHash} from 'node:crypto';
import {canonicalJson,parseSummaryRequest,parseSummaryResult} from './omp_bridge/contract/logs_v1.ts';
const wire=JSON.parse(fs.readFileSync(0,'utf8'));
const hash=(value)=>createHash('sha256').update(canonicalJson(value),'utf8').digest('hex');
process.stdout.write(JSON.stringify({request:hash(parseSummaryRequest(wire.request)),result:hash(parseSummaryResult(wire.result))}));"""
        self.assertEqual(self.node(program, self.golden), expected)

    def test_both_languages_reject_wrong_identity_range_version_and_extra_fields(self) -> None:
        request = self.golden["request"]
        result = self.golden["result"]
        cases: list[tuple[str, dict]] = []
        for kind, mutate in (
            ("request", lambda row: row.update(schemaVersion=2)),
            ("request", lambda row: row.update(requestId="../bad")),
            ("request", lambda row: row.update(unexpected=True)),
            ("request", lambda row: row["sources"][0].update(end=row["sources"][0]["start"])),
            ("request", lambda row: row["sources"][0]["source"].update(revision=999)),
            ("request", lambda row: row.update(inputKey="0" * 64)),
            ("result", lambda row: row.update(requestId="../bad")),
            ("result", lambda row: row.update(extra="not v1")),
            ("result", lambda row: row["usage"].update(inputTokens=True)),
            ("result", lambda row: row["usage"].update(costUsd=0.5)),
        ):
            candidate = copy.deepcopy(request if kind == "request" else result)
            mutate(candidate)
            cases.append((kind, candidate))
        for kind, candidate in cases:
            with self.subTest(kind=kind, candidate=str(candidate)[:80]), self.assertRaises(LogContractError):
                (parse_summary_request if kind == "request" else parse_summary_result)(candidate)
        program = """import fs from 'node:fs'; import {parseSummaryRequest,parseSummaryResult} from './omp_bridge/contract/logs_v1.ts';
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(([kind,value])=>{try {(kind==='request'?parseSummaryRequest:parseSummaryResult)(value);return true;}catch{return false;}})));"""
        self.assertEqual(self.node(program, cases), [False] * len(cases))

    def test_duplicate_nonfinite_and_noncanonical_json_are_rejected(self) -> None:
        payloads = (
            '{"schemaVersion":1,"kind":"task","kind":"command","id":"00000000-0000-0000-0000-000000000001"}',
            '{"value":NaN}', '{"value":Infinity}', '{"value":9007199254740992}',
        )
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(LogContractError):
                decode_json(payload)
        program = """import fs from 'node:fs'; import {decodeJson} from './omp_bridge/contract/logs_v1.ts';
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(raw=>{try{decodeJson(raw);return true;}catch{return false;}})));"""
        self.assertEqual(self.node(program, payloads), [False] * len(payloads))

    def test_result_envelope_and_path_safe_subject_boundary(self) -> None:
        result = copy.deepcopy(self.golden["result"])
        result["facts"] = ["가" * 8192]
        with self.assertRaises(LogContractError):
            parse_summary_result(result)
        subject = copy.deepcopy(self.golden["request"]["subject"])
        subject["id"] = "../../logs"
        with self.assertRaises(LogContractError):
            parse_subject_ref(subject)
        span = copy.deepcopy(self.golden["request"]["sources"][0])
        span["start"] = True
        with self.assertRaises(LogContractError):
            parse_source_range(span)

    def test_unicode_surrogates_and_invalid_calendar_dates_reject_in_both_languages(self) -> None:
        cases: list[tuple[str, dict]] = []
        for kind, field, value in (
            ("result", "facts", ["broken\ud800text"]),
            ("request", "requestedAt", "2026-02-30T01:02:03Z"),
            ("request", "requestedAt", "0000-01-01T01:02:03Z"),
        ):
            candidate = copy.deepcopy(self.golden[kind])
            candidate[field] = value
            cases.append((kind, candidate))
        for kind, candidate in cases:
            with self.subTest(kind=kind, field=str(candidate.get("requestedAt", "facts"))), self.assertRaises(LogContractError):
                (parse_summary_request if kind == "request" else parse_summary_result)(candidate)
        with self.assertRaises(LogContractError):
            canonical_json({"unsafe": "\ud800"})
        program = """import fs from 'node:fs'; import {parseSummaryRequest,parseSummaryResult,canonicalJson} from './omp_bridge/contract/logs_v1.ts';
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(([kind,value])=>{try{(kind==='request'?parseSummaryRequest:parseSummaryResult)(value);canonicalJson(value);return true;}catch{return false;}})));"""
        self.assertEqual(self.node(program, cases), [False] * len(cases))

    def test_observation_boundary_and_coverage_shared_schema(self) -> None:
        source = copy.deepcopy(self.golden["request"]["sources"][0]["source"])
        start = {"schemaVersion": 1, "eventId": "00000000-0000-0000-0000-000000000101",
                 "source": source, "kind": "start", "offset": 0, "observedAt": "2026-10-09T01:02:03Z"}
        final = {**start, "eventId": "00000000-0000-0000-0000-000000000102",
                 "kind": "final_drain", "offset": 3, "observedAt": "2026-10-09T01:02:04Z"}
        coverage = {"schemaVersion": 1, "coverageId": "00000000-0000-0000-0000-000000000103",
                    "source": source, "startEventId": start["eventId"],
                    "finalEventId": final["eventId"], "finalOffset": 3,
                    "sourceIds": ["00000000-0000-0000-0000-000000000104"]}
        self.assertEqual(parse_observation_boundary(start), start)
        self.assertEqual(parse_observation_boundary(final), final)
        self.assertEqual(parse_observation_coverage(coverage), coverage)
        cases: list[tuple[str, dict]] = []
        for kind, mutate in (
            ("boundary", lambda row: row.update(offset=1)),
            ("boundary", lambda row: row.update(schemaVersion=2)),
            ("boundary", lambda row: row.update(unexpected=True)),
            ("coverage", lambda row: row.update(finalEventId=row["startEventId"])),
            ("coverage", lambda row: row.update(sourceIds=[row["sourceIds"][0]] * 2)),
        ):
            row = copy.deepcopy(start if kind == "boundary" else coverage)
            mutate(row)
            cases.append((kind, row))
        for kind, row in cases:
            with self.subTest(kind=kind, row=row), self.assertRaises(LogContractError):
                (parse_observation_boundary if kind == "boundary" else parse_observation_coverage)(row)
        program = """import fs from 'node:fs'; import {parseObservationBoundary,parseObservationCoverage} from './omp_bridge/contract/logs_v1.ts';
const rows=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(rows.map(([kind,row])=>{try{(kind==='boundary'?parseObservationBoundary:parseObservationCoverage)(row);return true;}catch{return false;}})));"""
        self.assertEqual(self.node(program, [("boundary", start), ("boundary", final), ("coverage", coverage), *cases]),
                         [True, True, True, *([False] * len(cases))])

    def test_structured_source_ids_are_distinct_and_terminal_aliases_rejected_in_both_languages(self) -> None:
        base = copy.deepcopy(self.golden["request"]["sources"][0]["source"])
        manager = {**base, "sourceKind": "manager_instruction",
                   "messageId": "00000000-0000-0000-0000-000000000201"}
        report = {**base, "sourceKind": "report",
                  "eventId": "00000000-0000-0000-0000-000000000202"}
        terminal = copy.deepcopy(base)
        for source in (manager, report, terminal):
            self.assertEqual(parse_source_ref(source), source)
        invalid = []
        for mutate in (
            lambda row: row.update(messageId=str(manager["messageId"])),
            lambda row: row.update(eventId=str(report["eventId"])),
            lambda row: row.update(commandId="00000000-0000-0000-0000-000000000203"),
        ):
            row = copy.deepcopy(terminal)
            mutate(row)
            invalid.append(row)
        both_structured = {**manager, "eventId": report["eventId"]}
        invalid.append(both_structured)
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(LogContractError):
                parse_source_ref(source)
        program = """import fs from 'node:fs'; import {parseSourceRef} from './omp_bridge/contract/logs_v1.ts';
const values=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(values.map(v=>{try{parseSourceRef(v);return true;}catch{return false;}})));"""
        self.assertEqual(self.node(program, [manager, report, terminal, *invalid]),
                         [True, True, True, *([False] * len(invalid))])


if __name__ == "__main__":
    unittest.main()
